// The sun in the price chart: the dashboard's `sun` block (measured before now, the plan's corrected forecast after)
// made into points on the chart's own time axis, and the runs the renderer draws as one light area with a line on
// top. The sun has its own kW scale, never the price's: the tallest hour reaches `SUN_HEIGHT_SHARE` of the plot, so
// the price marks and their axis are untouched. Pure: no clock, no DOM.

import type { ChartSeries } from "./chart";
import type { SunBlock } from "./validate";

const HOUR_MS = 3_600_000;

/** How much of the plot's height the tallest sun hour reaches. */
export const SUN_HEIGHT_SHARE = 0.8;

/** One hour of sun: its span, its mean power in kW, and where the chart draws its middle (0 left edge, 1 right edge). */
export interface SunPoint {
  startMs: number;
  endMs: number;
  kw: number;
  /** Measured production (before the running hour) rather than the forecast. */
  measured: boolean;
  position: number;
}

export interface SunSeries {
  /** Ascending by time, only the hours the chart's axis shows. */
  points: SunPoint[];
  /** The tallest hour in kW, at least a small floor so a grey day does not fill the plot. */
  maxKw: number;
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
  for (const hour of block.hours) {
    const endMs = hour.startMs + HOUR_MS;
    if (!time.shows(hour.startMs, endMs)) {
      continue;
    }
    if (time.overlay && (today === null || hour.startMs < today.startMs || hour.startMs >= today.startMs + today.lengthMs)) {
      continue;
    }
    points.push({
      startMs: hour.startMs,
      endMs,
      kw: hour.kw,
      measured: hour.measured,
      position: time.position(hour.startMs + HOUR_MS / 2),
    });
  }
  if (points.length === 0) {
    return null;
  }
  points.sort((left, right) => left.startMs - right.startMs);
  return { points, maxKw: Math.max(MIN_MAX_KW, ...points.map((point) => point.kw)) };
}

/** Runs of hours that follow each other; a missing hour ends a run, so a gap is a gap in the drawing. */
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

/** The hour the sun has under an instant (a price mark's start), or `null`. */
export function sunAt(sun: SunSeries | null, instantMs: number): SunPoint | null {
  return sun?.points.find((point) => point.startMs <= instantMs && instantMs < point.endMs) ?? null;
}
