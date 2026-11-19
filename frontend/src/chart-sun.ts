// The sun in the price chart: the dashboard's `sun` block (measured before now, the plan's corrected forecast after)
// made into points on the chart's own time axis, and the runs the renderer draws as one light area with a line on
// top. The sun has its own kW scale, never the price's: the site's peak production (or, with none known, the day's
// own tallest hour) reaches `SUN_HEIGHT_SHARE` of the plot, so a dull day looks small and the price marks and their
// axis are untouched. Pure: no clock, no DOM.

import type { ChartSeries } from "./chart";
import type { SunBlock } from "./validate";

const HOUR_MS = 3_600_000;
const QUARTER_MS = 900_000;

/** How much of the plot's height the tallest sun hour reaches. */
export const SUN_HEIGHT_SHARE = 0.8;

/** One span of sun (an hour, or a measured quarter hour): its span, its mean power in kW, and where the chart draws its middle (0 left edge, 1 right edge). */
export interface SunPoint {
  startMs: number;
  endMs: number;
  kw: number;
  /** Measured production (up to now) rather than the forecast. */
  measured: boolean;
  position: number;
}

export interface SunSeries {
  /** Ascending by time, only the hours the chart's axis shows. */
  points: SunPoint[];
  /** The scale's top in kW: the site's peak, or the day's tallest hour where it is higher or no peak is known; at least a small floor. */
  maxKw: number;
  /** The site's peak production in kW when the block states one (the scale is at least this): the chart names it. */
  peakKw: number | null;
}

/** The scale's floor in kW: a day of 0.1 kW is drawn small, not as the whole plot. */
const MIN_MAX_KW = 0.5;

/**
 * The hours of the block the chart shows. On the wall-clock axis (today and tomorrow overlaid) only the day the now
 * fact is in, so tomorrow's sun never lands on today's hours; on the real-time range every hour in view.
 */
export function sunSeries(block: SunBlock | null, series: ChartSeries): SunSeries | null {
  if (block === null) {
    return null;
  }
  const time = series.time;
  const today = series.days.find((day) => day.role === "today") ?? null;
  const points: SunPoint[] = [];
  const add = (startMs: number, lengthMs: number, kw: number, measured: boolean): void => {
    const endMs = startMs + lengthMs;
    if (!time.shows(startMs, endMs)) {
      return;
    }
    if (time.overlay && (today === null || startMs < today.startMs || startMs >= today.startMs + today.lengthMs)) {
      return;
    }
    points.push({ startMs, endMs, kw, measured, position: time.position(startMs + lengthMs / 2) });
  };
  // The measured quarters replace the hours they cover; the forecast starts where the last quarter ends.
  const quarters = block.quarters;
  const covered = new Set(quarters.map((quarter) => Math.floor(quarter.startMs / HOUR_MS)));
  const measuredUntil = quarters.reduce((latest, quarter) => Math.max(latest, quarter.startMs + QUARTER_MS), 0);
  for (const hour of block.hours) {
    if (hour.measured ? covered.has(Math.floor(hour.startMs / HOUR_MS)) : hour.startMs < measuredUntil) {
      continue;
    }
    add(hour.startMs, HOUR_MS, hour.kw, hour.measured);
  }
  for (const quarter of quarters) {
    add(quarter.startMs, QUARTER_MS, quarter.kw, true);
  }
  if (points.length === 0) {
    return null;
  }
  points.sort((left, right) => left.startMs - right.startMs);
  const peakKw = block.peakKw !== null && block.peakKw > 0 ? block.peakKw : null;
  // A record hour above the stated peak still fits.
  return { points, maxKw: Math.max(MIN_MAX_KW, peakKw ?? 0, ...points.map((point) => point.kw)), peakKw };
}

/** Runs of spans that follow each other (an hour apart at most); a missing hour ends a run, so a gap is a gap. */
export function sunRuns(points: readonly SunPoint[]): SunPoint[][] {
  const runs: SunPoint[][] = [];
  for (const point of points) {
    const run = runs[runs.length - 1];
    const last = run?.[run.length - 1];
    if (run !== undefined && last !== undefined && point.startMs - last.startMs <= HOUR_MS) {
      run.push(point);
    } else {
      runs.push([point]);
    }
  }
  return runs;
}

/**
 * The runs to draw: `sunRuns` without the dark hours. An hour at 0 kW is drawn only beside one with sun, so the line
 * starts from and ends on the baseline at dawn and dusk; a night of zeros draws nothing.
 */
export function litRuns(points: readonly SunPoint[]): SunPoint[][] {
  const out: SunPoint[][] = [];
  for (const run of sunRuns(points)) {
    let current: SunPoint[] = [];
    run.forEach((point, index) => {
      const lit = point.kw > 0 || (run[index - 1]?.kw ?? 0) > 0 || (run[index + 1]?.kw ?? 0) > 0;
      if (lit) {
        current.push(point);
      } else if (current.length > 0) {
        out.push(current);
        current = [];
      }
    });
    if (current.length > 0) {
      out.push(current);
    }
  }
  return out;
}

/** The hour the sun has under an instant (a price mark's start), or `null`. */
export function sunAt(sun: SunSeries | null, instantMs: number): SunPoint | null {
  return sun?.points.find((point) => point.startMs <= instantMs && instantMs < point.endMs) ?? null;
}
