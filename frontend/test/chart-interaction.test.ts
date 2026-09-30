// The interaction controller: pointer, touch, keyboard and resize ownership.
//
// Every event is constructed and dispatched directly -- no sleeps, no fake timers, no real layout:
// the SVG maps a client coordinate 1:1 because jsdom reports a zero-sized rect, which is exactly the
// deterministic fallback `chartLocalPoint` documents.

import { describe, expect, it, vi } from "vitest";

import { nearestMark, walkMarks, type ChartMark, type ChartPoint } from "../src/chart";
import {
  createChartInteraction,
  nearestIntervalTo,
  type ChartInteraction,
  type ChartObserverLike,
  type ChartSize,
} from "../src/chart-interaction";
import type { ChartRenderResult } from "../src/chart-render";
import type { CardModel } from "../src/model";
import {
  AFTER_MIDNIGHT_MS,
  autumnModel,
  baseModel,
  fakeObservers,
  midnightModel,
  NEXT_DAY,
  quarterModel,
  renderFor,
  SEPT,
  twoDayModel,
} from "./visual-fixtures";

const SIZE: ChartSize = { width: 320, height: 150 };
const NOW = Date.parse("2026-09-22T04:30:00Z");

interface Harness {
  model: CardModel;
  /** The outer graph section: where the listeners live. */
  host: HTMLElement;
  /** The measured box: the SVG's own container, so the readout and legend cannot inflate it. */
  viewport: HTMLElement;
  drawn: ChartRenderResult;
  readout: HTMLElement;
  interaction: ChartInteraction;
  selections: Array<ChartMark | null>;
  resizes: ChartSize[];
  captures: number;
  releases: number;
}

function pointer(
  type: string,
  clientX: number,
  clientY: number,
  target: EventTarget,
): PointerEvent {
  const event = new PointerEvent(type, { clientX, clientY, bubbles: true, cancelable: true, pointerId: 1 });
  target.dispatchEvent(event);
  return event;
}

function keyboard(key: string, target: EventTarget, shiftKey = false): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key, shiftKey, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

function harness(
  model: CardModel = baseModel(),
  options: {
    observe?: (host: HTMLElement, onResize: () => void) => ChartObserverLike | null;
    measure?: (viewport: HTMLElement) => ChartSize;
    /** The clock the keyboard's own fallback reads; the fixture's own now unless a test moves it. */
    nowMs?: number;
  } = {},
): Harness {
  const host = document.createElement("section");
  document.body.append(host);
  const viewport = document.createElement("div");
  const readout = document.createElement("p");
  host.append(viewport, readout);
  const drawn = renderFor(model, { width: SIZE.width, height: SIZE.height });
  viewport.append(drawn.element);
  const selections: Array<ChartMark | null> = [];
  const resizes: ChartSize[] = [];
  const state = { captures: 0, releases: 0 };
  // jsdom has no pointer capture: the two methods are stubbed so the ownership is observable.
  Object.defineProperty(host, "setPointerCapture", {
    value: () => {
      state.captures += 1;
    },
  });
  Object.defineProperty(host, "releasePointerCapture", {
    value: () => {
      state.releases += 1;
    },
  });
  const interaction = createChartInteraction({
    host,
    viewport,
    surface: () => viewport.querySelector("svg"),
    onSelectionChange: (mark) => selections.push(mark),
    onResize: (size) => resizes.push(size),
    measure: options.measure ?? (() => SIZE),
    observe: options.observe ?? (() => null),
    // The graph and its readout are the interactive region; a press elsewhere in the card clears.
    isInsideInteractive: (node) => node instanceof Node && (host.contains(node) || readout.contains(node)),
    now: () => options.nowMs ?? NOW,
  });
  interaction.setGeometry({
    scale: drawn.scale,
    targets: drawn.targets,
    marks: model.chart.marks,
    current: model.chart.current,
  });
  return {
    model,
    host,
    viewport,
    drawn,
    readout,
    interaction,
    selections,
    resizes,
    get captures() {
      return state.captures;
    },
    get releases() {
      return state.releases;
    },
  } as Harness;
}

function at(target: { x: number; y: number }): ChartPoint {
  return { x: target.x, y: target.y };
}

describe("pointer and touch", () => {
  it("selects the nearest visible ink on a press, and reports it once", () => {
    const h = harness();
    const target = h.drawn.targets[1];
    expect(target).toBeDefined();
    if (target === undefined) {
      return;
    }
    pointer("pointerdown", target.x, target.y, h.host);
    expect(h.interaction.selection()?.startMs).toBe(target.mark.startMs);
    expect(h.selections).toHaveLength(1);
    expect(h.selections[0]?.startMs).toBe(target.mark.startMs);
  });

  it("leaves the selection alone when the press lands in empty space", () => {
    const h = harness();
    const target = h.drawn.targets[0];
    if (target === undefined) {
      return;
    }
    pointer("pointerdown", target.x, target.y, h.host);
    const before = h.selections.length;
    pointer("pointerdown", 10, 140, h.host);
    expect(h.interaction.selection()?.startMs).toBe(target.mark.startMs);
    expect(h.selections).toHaveLength(before);
  });

  it("walks marks continuously during a horizontal drag and captures the pointer", () => {
    // A flat series, so a horizontal drag is unambiguously horizontal: on a steep series the accepted
    // rule (`nearestMark`, which is two-dimensional) needs the finger to trace the series itself,
    // and a mostly-vertical movement is exactly the scroll gesture this controller must not consume.
    const h = harness(quarterModel(() => ({}), [100, 100.2, 100.1, 100.05]));
    const first = h.drawn.targets[0];
    const later = h.drawn.targets[2];
    if (first === undefined || later === undefined) {
      return;
    }
    pointer("pointerdown", first.x, first.y, h.host);
    const before = h.selections.length;
    // A drag stays at the finger's own y: an early vertical move is a scroll, not a walk.
    const along = { x: later.x, y: first.y };
    pointer("pointermove", along.x, along.y, h.host);
    expect(h.captures).toBe(1);
    const expected = nearestMark(h.drawn.targets, at(along));
    expect(h.interaction.selection()?.startMs).toBe(expected?.startMs ?? -1);
    expect(h.selections).toHaveLength(before + 1);
    const third = h.drawn.targets[3];
    if (third === undefined) {
      return;
    }
    pointer("pointermove", third.x, first.y, h.host);
    // The walk keeps following the finger, one mark at a time, in chronological order.
    expect(h.selections).toHaveLength(before + 2);
    expect(h.interaction.selection()?.startMs).toBe(nearestMark(h.drawn.targets, at(third))?.startMs ?? -1);
    expect(h.interaction.selection()?.startMs).toBeGreaterThan(expected?.startMs ?? 0);
  });

  it("lets a vertical gesture scroll instead of selecting, and does not consume it", () => {
    const h = harness();
    const first = h.drawn.targets[0];
    if (first === undefined) {
      return;
    }
    pointer("pointerdown", first.x, first.y, h.host);
    const before = h.selections.length;
    const event = pointer("pointermove", first.x + 1, first.y + 60, h.host);
    expect(event.defaultPrevented).toBe(false);
    expect(h.captures).toBe(0);
    expect(h.selections).toHaveLength(before);
    // The gesture is abandoned: a later sideways move does not resurrect it.
    const after = pointer("pointermove", first.x + 120, first.y + 60, h.host);
    expect(after.defaultPrevented).toBe(false);
    expect(h.selections).toHaveLength(before);
  });

  it("releases a captured pointer on pointercancel and on pointerup", () => {
    const h = harness();
    const first = h.drawn.targets[0];
    const later = h.drawn.targets[2];
    if (first === undefined || later === undefined) {
      return;
    }
    pointer("pointerdown", first.x, first.y, h.host);
    pointer("pointermove", later.x, first.y, h.host);
    expect(h.captures).toBe(1);
    pointer("pointercancel", later.x, first.y, h.host);
    expect(h.releases).toBe(1);
    pointer("pointerdown", first.x, first.y, h.host);
    pointer("pointermove", later.x, first.y, h.host);
    pointer("pointerup", later.x, first.y, h.host);
    expect(h.releases).toBe(2);
  });

  it("clears on a press outside the graph, but not on a press inside it or in the readout", () => {
    const h = harness();
    const target = h.drawn.targets[0];
    if (target === undefined) {
      return;
    }
    pointer("pointerdown", target.x, target.y, h.host);
    pointer("pointerdown", 5, 5, h.readout);
    expect(h.interaction.selection()).not.toBeNull();
    pointer("pointerdown", 5, 5, h.drawn.element);
    expect(h.interaction.selection()).not.toBeNull();
    pointer("pointerdown", 5, 5, document.body);
    expect(h.interaction.selection()).toBeNull();
  });
});

describe("keyboard and accessibility", () => {
  it("starts at an edge, walks chronologically and stops at the ends", () => {
    const h = harness(twoDayModel());
    const marks = h.model.chart.marks;
    expect(marks.length).toBeGreaterThan(3);
    const right = keyboard("ArrowRight", h.viewport);
    expect(right.defaultPrevented).toBe(true);
    expect(h.interaction.selection()?.startMs).toBe(marks[0]?.startMs);
    keyboard("ArrowRight", h.viewport);
    expect(h.interaction.selection()?.startMs).toBe(marks[1]?.startMs);
    keyboard("End", h.viewport);
    const last = marks[marks.length - 1];
    expect(h.interaction.selection()?.startMs).toBe(last?.startMs);
    // At the end of the walk the selection stays where it is rather than wrapping.
    keyboard("ArrowRight", h.viewport);
    expect(h.interaction.selection()?.startMs).toBe(last?.startMs);
    keyboard("Home", h.viewport);
    expect(h.interaction.selection()?.startMs).toBe(marks[0]?.startMs);
    keyboard("ArrowLeft", h.viewport);
    expect(h.interaction.selection()?.startMs).toBe(marks[0]?.startMs);
  });

  it("reaches both passes of a repeated autumn hour, in chronological order", () => {
    const h = harness(autumnModel());
    const repeated = h.model.chart.marks.filter((mark) => mark.wallClock === "02:00");
    expect(repeated).toHaveLength(2);
    keyboard("End", h.viewport);
    expect(h.interaction.selection()?.startMs).toBe(h.model.chart.marks[h.model.chart.marks.length - 1]?.startMs);
    keyboard("Home", h.viewport);
    const walked: number[] = [];
    for (let step = 0; step < h.model.chart.marks.length; step += 1) {
      walked.push(h.interaction.selection()?.startMs ?? -1);
      keyboard("ArrowRight", h.viewport);
    }
    const distinct = Array.from(new Set(walked));
    expect(distinct).toHaveLength(h.model.chart.marks.length);
    expect(distinct).toEqual(h.model.chart.marks.map((mark) => mark.startMs));
  });

  it("clears with Escape and selects with Enter or Space", () => {
    const h = harness();
    keyboard("ArrowRight", h.viewport);
    const selected = h.interaction.selection();
    expect(selected).not.toBeNull();
    keyboard("Escape", h.viewport);
    expect(h.interaction.selection()).toBeNull();
    const enter = keyboard("Enter", h.viewport);
    expect(enter.defaultPrevented).toBe(true);
    // The current interval exists in this fixture, so Enter selects exactly it.
    expect(h.interaction.selection()?.startMs).toBe(h.model.chart.current?.startMs);
    keyboard("Escape", h.viewport);
    const back = keyboard(" ", h.viewport);
    expect(back.defaultPrevented).toBe(true);
    expect(h.interaction.selection()?.startMs).toBe(h.model.chart.current?.startMs);
  });

  it("falls back to the nearest interval to now when there is no current interval", () => {
    const model = baseModel(Date.parse("2026-09-22T09:00:00Z"));
    expect(model.chart.current).toBeNull();
    const h = harness(model);
    keyboard("Enter", h.viewport);
    const nearest = nearestIntervalTo(model.chart.marks, NOW);
    expect(h.interaction.selection()?.startMs).toBe(nearest?.startMs);
    // It is called "nearest", never "current": the current mark is still absent.
    expect(model.chart.current).toBeNull();
    expect(h.interaction.selection()?.startMs).not.toBe(undefined);
  });

  it("ignores keys it does not own, and keeps an existing selection on Enter", () => {
    const h = harness();
    const ignored = keyboard("a", h.viewport);
    expect(ignored.defaultPrevented).toBe(false);
    expect(h.interaction.selection()).toBeNull();
    keyboard("ArrowRight", h.viewport);
    const first = h.interaction.selection()?.startMs;
    keyboard("ArrowRight", h.viewport);
    const second = h.interaction.selection()?.startMs;
    expect(second).not.toBe(first);
    keyboard("Enter", h.viewport);
    expect(h.interaction.selection()?.startMs).toBe(second);
    expect(walkMarks(h.model.chart.marks, null, 1)?.startMs).toBe(h.model.chart.marks[0]?.startMs);
  });

  it("selects and walks the date the clock is in, across local midnight", () => {
    const model = midnightModel();
    expect(model.chart.current?.dayKey).toBe(NEXT_DAY);
    const h = harness(model, { nowMs: AFTER_MIDNIGHT_MS });
    // The drawn picture already marks the post-midnight interval as the current one...
    expect(h.drawn.targets.filter((target) => target.current).map((target) => target.mark.dayKey)).toEqual([
      NEXT_DAY,
    ]);
    // ...so Enter with nothing selected selects that interval, not the expired quarter the capture also
    // holds and not some nearest-mark guess.
    keyboard("Enter", h.viewport);
    expect(h.interaction.selection()?.startMs).toBe(model.chart.current?.startMs);
    expect(h.interaction.selection()?.wallClock).toBe("00:00");
    // The walk still visits every record of both dates, in instant order: Home reaches the expired one,
    // and one step to the right crosses midnight into today's.
    keyboard("Escape", h.viewport);
    keyboard("Home", h.viewport);
    expect(h.interaction.selection()?.dayKey).toBe(SEPT);
    expect(h.interaction.selection()?.wallClock).toBe("23:45");
    keyboard("ArrowRight", h.viewport);
    expect(h.interaction.selection()?.dayKey).toBe(NEXT_DAY);
    expect(h.interaction.selection()?.wallClock).toBe("00:00");
    keyboard("ArrowLeft", h.viewport);
    expect(h.interaction.selection()?.dayKey).toBe(SEPT);
    expect(h.interaction.selection()?.wallClock).toBe("23:45");
  });
});

describe("resize ownership", () => {
  it("redraws on a real change and coalesces a redundant equal size", () => {
    const observers = fakeObservers();
    const h = harness(baseModel(), { observe: observers.factory });
    expect(observers.created).toHaveLength(1);
    expect(h.resizes).toHaveLength(0);
    observers.created[0]?.fire();
    expect(h.resizes).toHaveLength(0); // The same 320x150: not a redraw.
    const h2 = harness(baseModel(), {
      observe: observers.factory,
      measure: () => ({ width: 390, height: 150 }),
    });
    observers.created[1]?.fire();
    expect(h2.resizes).toEqual([{ width: 390, height: 150 }]);
    expect(observers.created[1]?.host).toBe(h2.viewport);
    const resizesAfter = h2.resizes.length;
    observers.created[1]?.fire();
    expect(h2.resizes).toHaveLength(resizesAfter);
  });

  it("takes one deterministic size when the platform has no observer", () => {
    const h = harness(baseModel(), { observe: () => null, measure: () => ({ width: 300, height: 160 }) });
    expect(h.interaction.isObserving()).toBe(false);
    expect(h.resizes).toEqual([{ width: 300, height: 160 }]);
    expect(h.interaction.size()).toEqual({ width: 300, height: 160 });
  });

  it("measures and observes the SVG viewport, never the graph with its text", () => {
    const observed: Element[] = [];
    const measured: Element[] = [];
    const h = harness(baseModel(), {
      observe: (node, onResize) => {
        observed.push(node);
        void onResize;
        return { disconnect: () => {} };
      },
      measure: (viewport) => {
        measured.push(viewport);
        // A browser-like measurement: the outer graph is tall because the readout and legend grew,
        // while the viewport itself is 150 px tall. Only the viewport may be consulted.
        return viewport === h.viewport ? { width: 320, height: 150 } : { width: 320, height: 260 };
      },
    });
    expect(observed).toEqual([h.viewport]);
    expect(measured.every((node) => node === h.viewport)).toBe(true);
    expect(h.interaction.size()).toEqual({ width: 320, height: 150 });
    expect(h.host.getBoundingClientRect().height).not.toBe(260);
  });

  it("does not redraw when the readout or legend grows", () => {
    const h = harness(baseModel(), { measure: () => ({ width: 320, height: 150 }) });
    const before = h.resizes.length;
    const readout = h.readout;
    for (let line = 0; line < 8; line += 1) {
      readout.append(document.createElement("span"));
      readout.append(document.createElement("br"));
    }
    // The width did not change, so the policy's height did not either: no geometry work at all.
    h.interaction.setGeometry({
      scale: h.drawn.scale,
      targets: h.drawn.targets,
      marks: h.model.chart.marks,
      current: h.model.chart.current,
    });
    keyboard("ArrowRight", h.viewport);
    expect(h.resizes).toHaveLength(before);
    expect(h.interaction.size()).toEqual({ width: 320, height: 150 });
  });

  it("disconnects on destroy, after which observer callbacks are inert", () => {
    const observers = fakeObservers();
    const h = harness(baseModel(), { observe: observers.factory });
    h.interaction.destroy();
    expect(observers.created[0]?.disconnects).toBe(1);
    expect(h.interaction.isObserving()).toBe(false);
    const resizes = h.resizes.length;
    observers.created[0]?.fire();
    expect(h.resizes).toHaveLength(resizes);
  });
});

describe("two instances are isolated", () => {
  it("never share selection, listeners or dimensions", () => {
    const first = harness(baseModel(), { measure: () => ({ width: 320, height: 150 }) });
    const second = harness(twoDayModel(), { measure: () => ({ width: 390, height: 170 }) });
    const target = first.drawn.targets[0];
    expect(target).toBeDefined();
    if (target === undefined) {
      return;
    }
    pointer("pointerdown", target.x, target.y, first.host);
    expect(first.interaction.selection()).not.toBeNull();
    expect(second.interaction.selection()).toBeNull();
    expect(first.interaction.size()).toEqual({ width: 320, height: 150 });
    expect(second.interaction.size()).toEqual({ width: 390, height: 170 });

    first.interaction.destroy();
    // The survivor keeps working: its listeners were never the other one's.
    keyboard("ArrowRight", second.viewport);
    expect(second.interaction.selection()).not.toBeNull();
    pointer("pointerdown", target.x, target.y, first.host);
    expect(first.selections).toHaveLength(1);
  });

  it("releases every listener on destroy", () => {
    const h = harness();
    const doc = h.host.ownerDocument;
    const added = vi.spyOn(doc, "addEventListener");
    const removed = vi.spyOn(doc, "removeEventListener");
    const second = harness();
    const target = second.drawn.targets[0];
    if (target !== undefined) {
      pointer("pointerdown", target.x, target.y, second.host);
    }
    second.interaction.destroy();
    const outsideAdds = added.mock.calls.filter((call) => call[0] === "pointerdown");
    const outsideRemoves = removed.mock.calls.filter((call) => call[0] === "pointerdown");
    expect(outsideAdds.length).toBeGreaterThan(0);
    expect(outsideRemoves.length).toBe(outsideAdds.length);
    expect(outsideRemoves.length).toBeGreaterThan(0);

    const selections = second.selections.length;
    if (target !== undefined) {
      pointer("pointerdown", target.x, target.y, second.host);
    }
    keyboard("ArrowRight", second.viewport);
    pointer("pointerdown", 5, 5, doc.body);
    expect(second.selections).toHaveLength(selections);
    expect(second.interaction.selection()).toBeNull();
    expect(h.interaction.selection()).toBeNull();
  });
});
