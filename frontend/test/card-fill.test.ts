// The card in a box of fixed height: the plan, a pure function of one reading of the natural layout.

import { describe, expect, it } from "vitest";

import { planFill, type FillReading } from "../src/card-fill";
import { chartHeightForWidth } from "../src/chart-render";
import { CHART_DENSITY } from "../src/visual-styles";

const WIDTH = 470;
const BASIS = chartHeightForWidth(WIDTH);
const MIN = CHART_DENSITY.normal.minHeight;
const LINE = 20;

describe("planFill", () => {
  const reading = (box: number, natural: number, statusLines = 1): FillReading => ({
    box,
    natural,
    statusHeight: statusLines * LINE,
    lineHeight: LINE,
  });

  it("changes nothing when the box is the card's own height or there is no layout", () => {
    expect(planFill(reading(400, 400), { basis: BASIS, min: MIN })).toBeNull();
    expect(planFill(reading(400.6, 400), { basis: BASIS, min: MIN })).toBeNull();
    expect(planFill(reading(0, 0), { basis: BASIS, min: MIN })).toBeNull();
  });

  it("gives a taller box's extra height to the chart", () => {
    const plan = planFill(reading(520, 400), { basis: BASIS, min: MIN });
    expect(plan).toEqual({ chartHeight: BASIS + 120, statusLines: null, spare: 0 });
  });

  it("shrinks the chart in a shorter box, down to its minimum, before touching the status", () => {
    expect(planFill(reading(399, 400, 4), { basis: BASIS, min: MIN })).toEqual({
      chartHeight: BASIS - 1,
      statusLines: null,
      spare: 0,
    });
  });

  it("then gives up whole status lines, keeping at least one, and the chart takes what they freed", () => {
    // Four lines, the chart 2 px above its minimum: 50 px short at the minimum takes three lines (60 px).
    const plan = planFill(reading(400 - 52, 400, 4), { basis: MIN + 2, min: MIN });
    expect(plan?.statusLines).toBe(1);
    expect(plan?.chartHeight).toBe(MIN + 10);
    // 10 px short at the minimum costs one line, and the chart gets the other 10.
    const one = planFill(reading(400 - 12, 400, 4), { basis: MIN + 2, min: MIN });
    expect(one).toEqual({ chartHeight: MIN + 10, statusLines: 3, spare: 0 });
    // Far too short: one line is kept and the chart stays at its minimum.
    const tiny = planFill(reading(100, 400, 4), { basis: MIN + 2, min: MIN });
    expect(tiny).toEqual({ chartHeight: MIN, statusLines: 1, spare: 0 });
  });

  it("does not clamp a long status in a tall box", () => {
    expect(planFill(reading(700, 400, 6), { basis: BASIS, min: MIN })?.statusLines).toBeNull();
  });

  it("puts the height under a collapsed chart above the action bar", () => {
    expect(planFill(reading(520, 400), { basis: 0, min: 0 })).toEqual({ chartHeight: 0, statusLines: null, spare: 120 });
  });

  it("is the same plan for the same reading", () => {
    const first = planFill(reading(380, 400, 3), { basis: BASIS, min: MIN });
    expect(planFill(reading(380, 400, 3), { basis: BASIS, min: MIN })).toEqual(first);
  });
});
