// The view's side of the one focus line, and the one interval-boundary appointment that moves it.
//
// Everything here is driven: the clock is a variable this test owns and the scheduler is a recording
// double, so no test waits, nothing fires late, and the cross-client fixture's own eight-minute
// boundary is asserted exactly rather than waited for.

import { describe, expect, it } from "vitest";

import { BOUNDARY_HORIZON_MS, createCardView, readoutTextFor, type CardView } from "../src/card-view";
import { chartNowAt, chartScale, intervalIdentity, xForWallClock } from "../src/chart";
import { translate } from "../src/i18n";
import { VISUAL_CLASSES } from "../src/visual-styles";
import {
  baseModel,
  crossClientModel,
  CROSS_BOUNDARY_MS,
  CROSS_CURRENT_FRACTION,
  CROSS_CURRENT_START,
  CROSS_NOW,
  CROSS_SELECTED_FRACTION,
  CROSS_SELECTED_START,
  AFTER_MIDNIGHT_MS,
  MIDNIGHT_MS,
  midnightModel,
  mountPoint,
  NEXT_DAY,
  SEPT,
  twoDayModel,
} from "./visual-fixtures";

const SIZE = { width: 320, height: 150 };

interface Harness {
  card: CardView;
  root: ShadowRoot;
  svg(): SVGSVGElement | null;
  line(className: string): SVGLineElement | null;
  legend(): string;
  description(): string;
  /** The summary line's own text: Max, Min and the live Now figure. */
  summary(): string;
  setNow(ms: number): void;
  /** The one pending appointment, or null when nothing is armed. */
  pending(): { delayMs: number; handle: number } | null;
  fire(): void;
  cleared(): number[];
}

/** The view with a clock and a scheduler this test drives: no waiting, and nothing fires late. */
function focusView(model = crossClientModel(), startMs = CROSS_NOW): Harness {
  let current = startMs;
  let handle = 0;
  const armed: Array<{ callback: () => void; delayMs: number; handle: number }> = [];
  const clearedHandles: number[] = [];
  const mount = mountPoint("focus");
  const card = createCardView({
    model,
    mount: mount.root,
    idPrefix: "focus",
    now: () => current,
    measure: () => SIZE,
    observe: () => null,
    onAction: () => {},
    onOpenSettings: () => {},
    onSaveSettings: () => {},
    onReloadSettings: () => {},
    onReapplySettings: () => {},
    // The fourth editor reports its own presses and choices the same way: the view is the subject.
    onOpenMarket: () => {},
    onSaveMarket: () => {},
    onReloadMarket: () => {},
    onReapplyMarket: () => {},
    onMarketAreaChange: () => {},
    isAdmin: true,
    onSelectStrategy: () => {},
    setTimer: (callback, delayMs) => {
      handle += 1;
      armed.push({ callback, delayMs, handle });
      return handle;
    },
    clearTimer: (cleared) => {
      clearedHandles.push(cleared);
      const index = armed.findIndex((entry) => entry.handle === cleared);
      if (index >= 0) {
        armed.splice(index, 1);
      }
    },
  });
  return {
    card,
    root: mount.root,
    // The chart's own SVG: the header's brand mark is an SVG too, and it is not the chart.
    svg: () => mount.root.querySelector(".spotnav-chart-viewport svg"),
    line: (className) => mount.root.querySelector<SVGLineElement>(`svg > .${className}`),
    legend: () => mount.root.querySelector(`.${VISUAL_CLASSES.legend}`)?.textContent ?? "",
    summary: () => mount.root.querySelector(`.${VISUAL_CLASSES.summary}`)?.textContent ?? "",
    description: () => mount.root.querySelector("svg > desc")?.textContent ?? "",
    setNow: (ms) => {
      current = ms;
    },
    pending: () => {
      const entry = armed[armed.length - 1];
      return entry === undefined ? null : { delayMs: entry.delayMs, handle: entry.handle };
    },
    fire: () => {
      const entry = armed[armed.length - 1];
      if (entry !== undefined) {
        armed.length = 0;
        entry.callback();
      }
    },
    cleared: () => clearedHandles.slice(),
  };
}
describe("the interval-boundary appointment", () => {
  it("waits exactly for the end of the interval the clock is in", () => {
    const harness = focusView();
    // 00:22 is inside 00:15-00:30, so the appointment is the eight minutes the fixture states.
    expect(harness.pending()?.delayMs).toBe(CROSS_BOUNDARY_MS);
    expect(CROSS_BOUNDARY_MS).toBe(CROSS_CURRENT_START + 15 * 60_000 - CROSS_NOW);
    harness.card.destroy();
  });

  it("redraws nothing inside the interval and redraws once at the boundary", () => {
    const model = crossClientModel();
    const harness = focusView(model);
    const scale = chartScale(
      SIZE.width,
      SIZE.height,
      model.chart.marks.map((mark) => mark.price),
    );
    const first = harness.svg();
    expect(first).not.toBeNull();
    expect(harness.line(VISUAL_CLASSES.now)?.hasAttribute("display")).toBe(false);
    expect(Number(harness.line(VISUAL_CLASSES.now)?.getAttribute("x1"))).toBeCloseTo(
      xForWallClock(scale, CROSS_CURRENT_FRACTION),
      9,
    );

    // A callback that fires inside the same interval changes nothing: no redraw, same line, same
    // geometry -- the appointment is simply re-armed from the clock it just read.
    harness.setNow(CROSS_NOW + 10_000);
    harness.fire();
    expect(harness.svg()).toBe(first);
    expect(Number(harness.line(VISUAL_CLASSES.now)?.getAttribute("x1"))).toBeCloseTo(
      xForWallClock(scale, CROSS_CURRENT_FRACTION),
      9,
    );
    expect(harness.pending()?.delayMs).toBe(CROSS_BOUNDARY_MS - 10_000);

    // The clock crossing the boundary is the one moment the picture changes -- and it is one redraw.
    const atBoundary = CROSS_CURRENT_START + 15 * 60_000;
    harness.setNow(atBoundary);
    harness.fire();
    expect(harness.svg()).not.toBe(first);
    const moved = harness.line(VISUAL_CLASSES.now);
    expect(moved?.hasAttribute("display")).toBe(false);
    expect(Number(moved?.getAttribute("x1"))).toBeCloseTo(
      xForWallClock(scale, CROSS_SELECTED_FRACTION),
      9,
    );
    expect(moved?.getAttribute("data-now-identity")).toBe(
      intervalIdentity(chartNowAt(model.chart.marks, atBoundary).mark),
    );
    expect(harness.pending()?.delayMs).toBe(15 * 60_000);
    harness.card.destroy();
  });

  it("arms nothing when nothing published today is still ahead of the clock", () => {
    // The base fixture's rows are all in the past relative to a clock a fortnight later.
    const harness = focusView(baseModel(), Date.parse("2026-10-06T04:30:00Z"));
    expect(harness.pending()).toBeNull();
    expect(harness.line(VISUAL_CLASSES.now)?.getAttribute("display")).toBe("none");
    harness.card.destroy();
  });

  it("arms nothing beyond the horizon, where the card's own refresh owns the redraw", () => {
    // The fixture's rows are weeks ahead of the clock: a 32-day timer would be clamped anyway.
    const far = Date.parse("2026-08-01T04:30:00Z");
    expect(Date.parse("2026-09-22T04:00:00Z") - far).toBeGreaterThan(BOUNDARY_HORIZON_MS);
    const harness = focusView(twoDayModel(), far);
    expect(harness.pending()).toBeNull();
    harness.card.destroy();
  });

  it("cancels the appointment on destroy, and a stale callback then owns nothing", () => {
    const harness = focusView();
    const handle = harness.pending()?.handle;
    harness.card.destroy();
    expect(harness.cleared()).toContain(handle);
    expect(harness.pending()).toBeNull();
    expect(harness.svg()).toBeNull();
    // A callback already in flight when the view went away must be inert: no throw, no redraw, no
    // re-arm, no second cancellation.
    expect(() => harness.fire()).not.toThrow();
    expect(harness.svg()).toBeNull();
    expect(harness.pending()).toBeNull();
    expect(harness.cleared().length).toBe(1);
  });
});
describe("the appointment across local midnight", () => {
  it("moves the now line once, at midnight, into the row the capture already published", () => {
    const start = MIDNIGHT_MS - 5 * 60_000;
    const model = midnightModel(start);
    const harness = focusView(model, start);
    // 23:55 is inside the date the clock is about to leave, so the appointment is the five minutes to
    // midnight -- where the old row ends and the new date's own first row begins.
    expect(harness.pending()?.delayMs).toBe(5 * 60_000);
    expect(harness.summary()).toContain("100 öre/kWh");
    const before = harness.svg();

    const atMidnight = MIDNIGHT_MS;
    harness.setNow(atMidnight);
    harness.fire();
    const now = chartNowAt(model.chart.marks, atMidnight);
    expect(now.mark?.dayKey).toBe(NEXT_DAY);
    expect(harness.svg()).not.toBe(before);
    expect(harness.line(VISUAL_CLASSES.now)?.hasAttribute("display")).toBe(false);
    expect(harness.line(VISUAL_CLASSES.now)?.getAttribute("data-now-identity")).toBe(now.identity);
    // The one now fact, in words and in the summary figure too: no read happened between the two.
    expect(harness.description()).toContain(readoutTextFor(model, now.mark));
    expect(harness.summary()).toContain("200 öre/kWh");
    // And the clock is armed for the new date's next boundary rather than stopping for the night.
    expect(harness.pending()?.delayMs).toBe(15 * 60_000);

    // The one now/selection-line rule still holds across the rollover: a selection replaces the solid
    // line, and clearing it restores the line on the interval the clock is now in.
    const expired = model.chart.marks.find((mark) => mark.dayKey === SEPT) ?? null;
    harness.card.chart().select(expired);
    expect(harness.line(VISUAL_CLASSES.now)?.getAttribute("display")).toBe("none");
    expect(harness.line(VISUAL_CLASSES.selection)?.hasAttribute("display")).toBe(false);
    harness.card.chart().select(null);
    expect(harness.line(VISUAL_CLASSES.now)?.hasAttribute("display")).toBe(false);
    expect(harness.line(VISUAL_CLASSES.now)?.getAttribute("data-now-identity")).toBe(now.identity);
    harness.card.destroy();
  });

  it("cancels the midnight appointment on destroy, and the stale callback then owns nothing", () => {
    const start = MIDNIGHT_MS - 5 * 60_000;
    const harness = focusView(midnightModel(start), start);
    const handle = harness.pending()?.handle;
    expect(handle).not.toBeUndefined();
    harness.card.destroy();
    expect(harness.cleared()).toContain(handle);
    expect(harness.pending()).toBeNull();
    expect(harness.svg()).toBeNull();
    // A callback a timer had already queued finds the view gone: no throw, no redraw, no re-arm.
    harness.setNow(MIDNIGHT_MS + 60_000);
    expect(() => harness.fire()).not.toThrow();
    expect(harness.svg()).toBeNull();
    expect(harness.pending()).toBeNull();
  });

  it("waits out the rest of the 00:00 quarter at 00:11, then advances and re-arms", () => {
    const model = midnightModel(AFTER_MIDNIGHT_MS);
    const harness = focusView(model, AFTER_MIDNIGHT_MS);
    expect(harness.pending()?.delayMs).toBe(4 * 60_000);
    expect(harness.summary()).toContain("200 öre/kWh");
    expect(harness.legend()).toContain(translate("en", "graph.legend.today"));
    expect(harness.legend()).toContain(translate("en", "graph.legend.past"));

    const atQuarter = MIDNIGHT_MS + 15 * 60_000;
    harness.setNow(atQuarter);
    harness.fire();
    expect(harness.line(VISUAL_CLASSES.now)?.getAttribute("data-now-identity")).toBe(
      intervalIdentity(chartNowAt(model.chart.marks, atQuarter).mark),
    );
    // Moved to 00:15-00:30, whose own price the summary figure now names, and armed for 00:30.
    expect(harness.summary()).toContain("300 öre/kWh");
    expect(harness.pending()?.delayMs).toBe(15 * 60_000);
    harness.card.destroy();
  });
});





describe("the focus line follows the selection", () => {
  it("shows the dashed line while selected and the solid now line once cleared", () => {
    const model = crossClientModel();
    const harness = focusView(model);
    expect(harness.line(VISUAL_CLASSES.now)?.hasAttribute("display")).toBe(false);
    expect(harness.line(VISUAL_CLASSES.selection)?.getAttribute("display")).toBe("none");

    const selected = model.chart.marks.find((mark) => mark.startMs === CROSS_SELECTED_START) ?? null;
    expect(selected).not.toBeNull();
    harness.card.chart().select(selected);
    expect(harness.line(VISUAL_CLASSES.now)?.getAttribute("display")).toBe("none");
    expect(harness.line(VISUAL_CLASSES.selection)?.hasAttribute("display")).toBe(false);
    expect(harness.card.selection()).toBe(selected);

    // Escape, from the viewport itself (the graph's one keyboard owner), clears and restores the now line.
    const viewport = harness.root.querySelector<HTMLElement>(`.${VISUAL_CLASSES.viewport}`);
    expect(viewport).not.toBeNull();
    expect(viewport?.tabIndex).toBe(0);
    viewport?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(harness.card.selection()).toBeNull();
    expect(harness.line(VISUAL_CLASSES.selection)?.getAttribute("display")).toBe("none");
    expect(harness.line(VISUAL_CLASSES.now)?.hasAttribute("display")).toBe(false);

    // A press outside the graph clears too.
    harness.card.chart().select(selected);
    document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    expect(harness.card.selection()).toBeNull();
    expect(harness.line(VISUAL_CLASSES.now)?.hasAttribute("display")).toBe(false);

    harness.card.destroy();
  });

  it("leaves no stale line state after a cancelled pointer gesture", () => {
    const harness = focusView();
    const graph = harness.root.querySelector<HTMLElement>(`.${VISUAL_CLASSES.graphSurface}`);
    const svg = harness.svg();
    // A cancelled press that never became a drag leaves the selection where the press left it; what
    // matters is that the drawn line still agrees with that selection and no appointment was lost.
    const before = harness.card.selection();
    graph?.dispatchEvent(new PointerEvent("pointercancel", { bubbles: true, pointerId: 1 }));
    expect(harness.card.selection()).toBe(before);
    if (before === null) {
      expect(harness.line(VISUAL_CLASSES.now)?.hasAttribute("display")).toBe(false);
      expect(harness.line(VISUAL_CLASSES.selection)?.getAttribute("display")).toBe("none");
    } else {
      expect(harness.line(VISUAL_CLASSES.now)?.getAttribute("display")).toBe("none");
      expect(harness.line(VISUAL_CLASSES.selection)?.hasAttribute("display")).toBe(false);
    }
    expect(harness.svg()).toBe(svg);
    expect(harness.pending()?.delayMs).toBe(CROSS_BOUNDARY_MS);
    harness.card.destroy();
  });
});

describe("the accessible text names the focus without announcing the lines", () => {
  it("names today, tomorrow and the current interval, and the selection while one is selected", () => {
    const model = crossClientModel();
    const harness = focusView(model);
    const legend = harness.legend();
    expect(legend).toContain(translate("en", "graph.legend.today"));
    expect(legend).toContain(translate("en", "graph.legend.tomorrow"));
    expect(legend).toContain(translate("en", "graph.legend.current"));
    expect(legend).not.toContain(translate("en", "graph.legend.selection"));

    // The description says which interval is current, in words.
    const marker = "\u0000";
    const prefix = (translate("en", "graph.descriptionNow", { now: marker }).split(marker)[0] as string).trim();
    expect(prefix.length).toBeGreaterThan(0);
    expect(harness.description()).toContain(prefix);

    const selected = model.chart.marks.find((mark) => mark.startMs === CROSS_SELECTED_START) ?? null;
    harness.card.chart().select(selected);
    expect(harness.legend()).toContain(translate("en", "graph.legend.selection"));
    expect(harness.description()).toContain(harness.card.readoutText());

    // Decorative lines are never announced: no class name, and nothing about the lines themselves.
    for (const text of [harness.legend(), harness.description()]) {
      expect(text).not.toContain(VISUAL_CLASSES.now);
      expect(text).not.toContain(VISUAL_CLASSES.selection);
    }
    harness.card.destroy();
  });
});
