// The card in a box of fixed height: how tall the chart is drawn and how many lines of the status
// sentence are shown, so the card fills the box and the action bar sits at its bottom edge.
//
// The plan is a pure function of one reading taken with the card laid out as it is anywhere else
// (chart at its width rule, status unclamped), never of the layout the plan itself produced. A second
// reading after the plan is applied is therefore the same reading, and the same plan: no grow/shrink loop.

/** One reading of the card's natural layout, in CSS pixels. */
export interface FillReading {
  /** The height the container gives the card: its natural height when the container has none of its own. */
  box: number;
  /** The card's height laid out as usual: chart at its width rule, status sentence in full. */
  natural: number;
  /** The status sentence's height in full, and its line height; zeros when there is no sentence. */
  statusHeight: number;
  lineHeight: number;
  /**
   * The height the card gives back in a box of fixed height: the readout line under the chart, which there is drawn
   * over the chart's empty bottom edge instead of below it. Zero (or left out) when nothing is given back.
   */
  reclaimed?: number;
}

/**
 * The chart's own minimum in a box of fixed height (about a third of its 360 px height at 650 px wide): the box
 * gives up its other height before the chart goes below it. It is lower than the chart's minimum for its width
 * (`density.minHeight`, which applies outside a box). The order in which a box too small gives space back:
 * the gaps (already small), then the status sentence's extra lines (down to `FILL_STATUS_LINES`, the full text
 * stays the title), only then the chart below this minimum, down to `FILL_FLOOR_CHART_PX`; below that the
 * sentence gives up its second line too.
 */
export const FILL_MIN_CHART_PX = 120;

/** The chart is never drawn lower than this in a box, however long the status is (the chart's own drawing minimum). */
export const FILL_FLOOR_CHART_PX = 100;

/** The status lines kept while the chart is still above its minimum is being weighed against them. */
export const FILL_STATUS_LINES = 2;

/** What the card needs besides the reading: the chart's usual height and the least it may shrink to. */
export interface FillChart {
  /** The chart's height in the natural layout (its width rule); 0 while the chart is collapsed. */
  basis: number;
  /** The chart's minimum in a box (`FILL_MIN_CHART_PX`); 0 while the chart is collapsed. */
  min: number;
  /** The hard floor, below the minimum, the chart gives up height down to once the status is clamped; default `min`. */
  floor?: number;
}

export interface FillPlan {
  /** The chart's height in whole CSS pixels; 0 while the chart is collapsed. */
  chartHeight: number;
  /** The status lines to show, or `null` for the whole sentence. */
  statusLines: number | null;
  /** Height left over below the content (a collapsed chart, or rounding); it goes above the action bar. */
  spare: number;
}

/**
 * Differences below this are layout rounding, not a box of another height. The reading's box and natural
 * heights are whole pixels, so one pixel of slack also keeps a box exactly as tall as the card ordinary.
 */
const SAME_HEIGHT_PX = 1;

/** A sliver of a line is not a line: a deficit this close to a whole number of lines needs no extra one. */
const LINE_EPSILON = 1e-3;

/**
 * The plan for one reading, or `null` when the card is not in a box of its own height (the container
 * leaves the height to the card, or gives it exactly its natural height): then nothing changes.
 *
 * The chart takes all the room the rest of the card leaves, down to `chart.min`. Below that the status
 * sentence gives up whole lines down to two, the chart takes what those freed; only then the chart goes
 * below its minimum, down to its floor, and below that the sentence keeps one line.
 */
export function planFill(reading: FillReading, chart: FillChart): FillPlan | null {
  const { box, natural } = reading;
  if (!(box > 0) || !(natural > 0) || Math.abs(box - natural) < SAME_HEIGHT_PX) {
    return null;
  }
  const others = natural - chart.basis - Math.max(0, reading.reclaimed ?? 0);
  let room = box - others;
  let statusLines = null as number | null;
  const floor = Math.min(chart.min, chart.floor ?? chart.min);
  const canClamp = reading.statusHeight > 0 && reading.lineHeight > 0;
  const lines = canClamp ? Math.max(1, Math.round(reading.statusHeight / reading.lineHeight)) : 1;
  // The clamped sentence is exactly `keep` lines high, whatever fraction the full one had over whole lines.
  const clamp = (keep: number): void => {
    if (keep < lines) {
      room += reading.statusHeight - keep * reading.lineHeight - (statusLines === null ? 0 : reading.statusHeight - statusLines * reading.lineHeight);
      statusLines = keep;
    }
  };
  if (room < chart.min && canClamp) {
    // First the sentence's extra lines, down to two.
    const deficit = chart.min - room;
    clamp(Math.max(Math.min(lines, FILL_STATUS_LINES), lines - Math.ceil(deficit / reading.lineHeight - LINE_EPSILON)));
    // Only then the chart below its minimum; below its floor the sentence's second line goes too.
    if (room < floor) {
      clamp(Math.max(1, (statusLines ?? lines) - Math.ceil((floor - room) / reading.lineHeight - LINE_EPSILON)));
    }
  }
  const chartHeight = chart.basis > 0 ? Math.max(floor, Math.floor(room)) : 0;
  return { chartHeight, statusLines, spare: Math.max(0, room - chartHeight) };
}
