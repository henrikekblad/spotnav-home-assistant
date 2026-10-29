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
}

/** What the card needs besides the reading: the chart's usual height and the least it may shrink to. */
export interface FillChart {
  /** The chart's height in the natural layout (its width rule); 0 while the chart is collapsed. */
  basis: number;
  /** The least height the chart is drawn at in a box; 0 while the chart is collapsed. */
  min: number;
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
 * sentence gives up whole lines, keeping at least one; the chart then takes what those lines freed.
 */
export function planFill(reading: FillReading, chart: FillChart): FillPlan | null {
  const { box, natural } = reading;
  if (!(box > 0) || !(natural > 0) || Math.abs(box - natural) < SAME_HEIGHT_PX) {
    return null;
  }
  const others = natural - chart.basis;
  let room = box - others;
  let statusLines: number | null = null;
  if (room < chart.min && reading.statusHeight > 0 && reading.lineHeight > 0) {
    const lines = Math.max(1, Math.round(reading.statusHeight / reading.lineHeight));
    const deficit = chart.min - room;
    const keep = Math.max(1, lines - Math.ceil(deficit / reading.lineHeight - LINE_EPSILON));
    if (keep < lines) {
      statusLines = keep;
      // The clamped sentence is exactly `keep` lines high, whatever fraction the full one had over whole lines.
      room += reading.statusHeight - keep * reading.lineHeight;
    }
  }
  const chartHeight = chart.basis > 0 ? Math.max(chart.min, Math.floor(room)) : 0;
  return { chartHeight, statusLines, spare: Math.max(0, room - chartHeight) };
}
