// The collapsed chart: a slim strip with the plan's charging periods as bars, the now line and the hour
// labels. Pure geometry on the chart's own axis, read from the same positions: on the wall-clock overlay
// 0 is 00:00 and 1 is 24:00 (one day or two overlaid), on the real-time range 0 is the current hour and 1
// the end of the last published day, with a divider at each midnight inside. So the strip spans exactly
// what the chart spans. The view turns it into DOM.

import { zoneClock, type ChartNow, type ChartSeries, type PlanBands } from "./chart";

/** The narrowest bar drawn, in CSS pixels, so a single quarter-hour still shows. */
export const STRIP_MIN_BAR_PX = 3;

const DAY_MS = 86_400_000;

export interface StripBar {
  kind: "installed" | "proposal";
  /** Positions on the chart's axis, like the chart's x. */
  start: number;
  end: number;
  /** Over by the now instant: drawn dimmed. */
  past: boolean;
  /** The band's own flag (another day on the wall-clock overlay, never on the range): drawn hatched like the graph's band. */
  tomorrow: boolean;
}

/**
 * The bars, in wall-clock order: every installed and proposed period, the part before `nowMs` marked
 * past. A period under way is split at the now instant, so what is done dims and what remains does not.
 * Tomorrow's periods keep their band's `tomorrow` flag, so they are hatched rather than solid.
 */
export function stripBars(bands: PlanBands, nowMs: number): StripBar[] {
  const bars: StripBar[] = [];
  for (const [kind, list] of [
    ["installed", bands.installed],
    ["proposal", bands.proposal],
  ] as const) {
    for (const band of list) {
      const { tomorrow } = band;
      if (band.endMs <= nowMs) {
        bars.push({ kind, start: band.startPosition, end: band.endPosition, past: true, tomorrow });
      } else if (band.startMs >= nowMs) {
        bars.push({ kind, start: band.startPosition, end: band.endPosition, past: false, tomorrow });
      } else {
        const share = (nowMs - band.startMs) / (band.endMs - band.startMs);
        const split = band.startPosition + share * (band.endPosition - band.startPosition);
        bars.push({ kind, start: band.startPosition, end: split, past: true, tomorrow });
        bars.push({ kind, start: split, end: band.endPosition, past: false, tomorrow });
      }
    }
  }
  return bars.sort((left, right) => left.start - right.start || left.end - right.end);
}

/** A share of the track as a CSS percentage, rounded so float noise never reaches the style. */
function percent(share: number): string {
  return `${Math.round(share * 1e6) / 1e4}%`;
}

export interface StripPlacement {
  left: string | null;
  right: string | null;
  width: string;
  minWidth: string;
}

/**
 * Where one bar sits in the track, as CSS lengths. A bar in the right half is anchored on its end, so
 * the minimum width grows inwards and a quarter-hour at 23:45 is not clipped by the track's edge.
 */
export function stripBarPlacement(bar: StripBar): StripPlacement {
  const width = percent(Math.max(0, bar.end - bar.start));
  const minWidth = `${STRIP_MIN_BAR_PX}px`;
  if (bar.start + bar.end > 1) {
    return { left: null, right: percent(1 - bar.end), width, minWidth };
  }
  return { left: percent(bar.start), right: null, width, minWidth };
}

/**
 * The now line's place: the current interval's position plus the time gone inside it (against `spanMs`,
 * the time the axis spans: the series' `time.spanMs`), or `null` when no published interval holds the
 * clock (the chart draws no now line then either).
 */
export function stripNowPosition(now: ChartNow, nowMs: number, spanMs: number = DAY_MS): number | null {
  const mark = now.mark;
  if (mark === null) {
    return null;
  }
  const inside = Math.min(Math.max(nowMs - mark.startMs, 0), mark.endMs - mark.startMs);
  return Math.min(Math.max(mark.position + inside / spanMs, 0), 1);
}

export interface StripTick {
  position: number;
  label: string;
  /** A midnight inside the range axis: the view draws a divider through the track there. */
  dayStart?: true;
}

/**
 * The hour labels under the strip, at the chart's own tick positions: `00 06 12 18 24` on the wall-clock
 * axis; on the range each tick's local hour in `timeZone`, the right edge `24`.
 */
export function stripTicks(series: ChartSeries, timeZone = ""): StripTick[] {
  const ticks =
    series.axis.length > 0
      ? series.axis
      : [0, 0.25, 0.5, 0.75, 1].map((position) => ({ position, instantMs: 0 }));
  const range = series.time?.kind === "range";
  const clock = zoneClock(timeZone);
  return ticks.map((tick) => {
    const hours =
      !range || tick.position >= 1
        ? Math.round(tick.position * 24)
        : Math.floor(clock.msOfDay(tick.instantMs) / 3_600_000);
    const label = String(hours).padStart(2, "0");
    return "dayStart" in tick && tick.dayStart === true
      ? { position: tick.position, label, dayStart: true }
      : { position: tick.position, label };
  });
}
