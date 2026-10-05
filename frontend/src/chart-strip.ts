// The collapsed chart: a slim 24-hour strip with the plan's charging periods as bars, the now line and
// the hour labels. Pure geometry on the chart's own wall-clock axis (0 is 00:00, 1 is 24:00), so the
// strip spans exactly what the chart spans, one day or two overlaid. The view turns it into DOM.

import type { ChartNow, ChartSeries, PlanBands } from "./chart";

/** The narrowest bar drawn, in CSS pixels, so a single quarter-hour still shows. */
export const STRIP_MIN_BAR_PX = 3;

const DAY_MS = 86_400_000;

export interface StripBar {
  kind: "installed" | "proposal";
  /** Wall-clock fractions of one day, like the chart's x. */
  start: number;
  end: number;
  /** Over by the now instant: drawn dimmed. */
  past: boolean;
}

/**
 * The bars, in wall-clock order: every installed and proposed period, the part before `nowMs` marked
 * past. A period under way is split at the now instant, so what is done dims and what remains does not.
 */
export function stripBars(bands: PlanBands, nowMs: number): StripBar[] {
  const bars: StripBar[] = [];
  for (const [kind, list] of [
    ["installed", bands.installed],
    ["proposal", bands.proposal],
  ] as const) {
    for (const band of list) {
      if (band.endMs <= nowMs) {
        bars.push({ kind, start: band.startPosition, end: band.endPosition, past: true });
      } else if (band.startMs >= nowMs) {
        bars.push({ kind, start: band.startPosition, end: band.endPosition, past: false });
      } else {
        const share = (nowMs - band.startMs) / (band.endMs - band.startMs);
        const split = band.startPosition + share * (band.endPosition - band.startPosition);
        bars.push({ kind, start: band.startPosition, end: split, past: true });
        bars.push({ kind, start: split, end: band.endPosition, past: false });
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
 * The now line's place: the current interval's position plus the time gone inside it, or `null` when no
 * published interval holds the clock (the chart draws no now line then either).
 */
export function stripNowPosition(now: ChartNow, nowMs: number): number | null {
  const mark = now.mark;
  if (mark === null) {
    return null;
  }
  const inside = Math.min(Math.max(nowMs - mark.startMs, 0), mark.endMs - mark.startMs);
  return Math.min(Math.max(mark.position + inside / DAY_MS, 0), 1);
}

/** The hour labels under the strip, at the chart's own tick positions: `00 06 12 18 24`. */
export function stripTicks(series: ChartSeries): Array<{ position: number; label: string }> {
  const positions = series.axis.length > 0 ? series.axis.map((tick) => tick.position) : [0, 0.25, 0.5, 0.75, 1];
  return positions.map((position) => ({
    position,
    label: String(Math.round(position * 24)).padStart(2, "0"),
  }));
}
