// The shared pointer policy: a mouse hovers, a touch taps, scrubs sideways or leaves a vertical gesture to the
// page; and every chart surface lets the page scroll vertically (`touch-action: pan-y`).

import { describe, expect, it, vi } from "vitest";

import { attachScrub, indexAtClientX, scrubBars } from "../src/chart-scrub";
import { HEAT_CLASSES, HEAT_STYLES } from "../src/heat-styles";
import { HOUSE_CLASSES, HOUSE_STYLES } from "../src/house-styles";
import { periodBars, type PeriodBar, type PeriodUi } from "../src/period-history";
import { VISUAL_CLASSES, VISUAL_STYLES } from "../src/visual-styles";

function send(target: EventTarget, type: string, clientX: number, clientY: number, pointerType = "touch"): PointerEvent {
  const event = new PointerEvent(type, { clientX, clientY, pointerType, pointerId: 1, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

/** The declarations of the first rule whose selector is exactly `.name`. */
function rule(styles: string, name: string): string {
  const match = new RegExp(`\\.${name} \\{([^}]*)\\}`).exec(styles);
  return match?.[1] ?? "";
}

describe("touch-action", () => {
  it("lets the page scroll vertically over every chart, and nothing else", () => {
    for (const [styles, name] of [
      [VISUAL_STYLES, VISUAL_CLASSES.graphSurface],
      [VISUAL_STYLES, VISUAL_CLASSES.historyBars],
      [HOUSE_STYLES, HOUSE_CLASSES.surface],
      [HEAT_STYLES, HEAT_CLASSES.daySurface],
    ] as const) {
      expect(rule(styles, name), name).toMatch(/touch-action: pan-y;/);
    }
  });
});

describe("attachScrub", () => {
  it("tells a tap, a sideways scrub and a scroll apart, and a mouse only hovers", () => {
    const host = document.createElement("div");
    const handlers = { hover: vi.fn(), leave: vi.fn(), press: vi.fn(), tap: vi.fn(), drag: vi.fn(), end: vi.fn() };
    const scrub = attachScrub(host, handlers);
    send(host, "pointerdown", 10, 10);
    send(host, "pointerup", 11, 10);
    expect(handlers.tap).toHaveBeenCalledTimes(1);
    send(host, "pointerdown", 10, 10);
    expect(send(host, "pointermove", 30, 12).defaultPrevented).toBe(true);
    send(host, "pointerup", 30, 12);
    expect(handlers.drag).toHaveBeenCalledTimes(1);
    expect(handlers.end).toHaveBeenCalledWith(expect.anything(), false);
    expect(handlers.tap).toHaveBeenCalledTimes(1);
    send(host, "pointerdown", 10, 10);
    expect(send(host, "pointermove", 11, 40).defaultPrevented).toBe(false);
    expect(send(host, "pointermove", 60, 40).defaultPrevented).toBe(false);
    send(host, "pointerup", 60, 40);
    expect(handlers.drag).toHaveBeenCalledTimes(1);
    expect(handlers.tap).toHaveBeenCalledTimes(1);
    send(host, "pointermove", 50, 5, "mouse");
    host.dispatchEvent(new PointerEvent("pointerleave", { pointerType: "mouse" }));
    expect(handlers.hover).toHaveBeenCalledTimes(1);
    expect(handlers.leave).toHaveBeenCalledTimes(1);
    expect(handlers.press).toHaveBeenCalledTimes(3);
    scrub.detach();
    send(host, "pointerdown", 10, 10);
    expect(handlers.press).toHaveBeenCalledTimes(3);
  });

  it("keeps the bar a scrub ends on, and nothing of a scrub the platform cancels", () => {
    const plot = document.createElement("div");
    const bars = [0, 1, 2].map((index) => {
      const bar = document.createElement("button");
      bar.getBoundingClientRect = () => ({ left: index * 10, right: index * 10 + 8, top: 0, bottom: 5, width: 8, height: 5, x: index * 10, y: 0, toJSON: () => ({}) });
      return bar;
    });
    const show = vi.fn();
    const choose = vi.fn();
    const restore = vi.fn();
    scrubBars(plot, bars, show, choose, restore);
    send(plot, "pointerdown", 2, 2);
    send(plot, "pointermove", 24, 2);
    send(plot, "pointercancel", 24, 2);
    expect(show).toHaveBeenLastCalledWith(2);
    expect(choose).not.toHaveBeenCalled();
    expect(restore).toHaveBeenCalledTimes(1);
    send(plot, "pointerdown", 2, 2);
    send(plot, "pointermove", 14, 2);
    send(plot, "pointerup", 14, 2);
    expect(choose).toHaveBeenCalledWith(1);
    expect(restore).toHaveBeenCalledTimes(1);
  });

  it("picks the element whose horizontal span is nearest", () => {
    const nodes = [0, 1, 2].map((index) => {
      const node = document.createElement("span");
      node.getBoundingClientRect = () => ({ left: index * 10, right: index * 10 + 8, top: 0, bottom: 5, width: 8, height: 5, x: index * 10, y: 0, toJSON: () => ({}) });
      return node;
    });
    expect(indexAtClientX(nodes, -5)).toBe(0);
    expect(indexAtClientX(nodes, 14)).toBe(1);
    expect(indexAtClientX(nodes, 19.5)).toBe(2);
    expect(indexAtClientX(nodes, 99)).toBe(2);
    expect(indexAtClientX([], 5)).toBeNull();
  });
});

describe("the history bars", () => {
  it("follow a touch drag in the readout and keep the bar it ends on", () => {
    const bars: PeriodBar[] = ["a", "b", "c", "d"].map((key) => ({ key, value: 1, dear: null, words: `bar ${key}`, axis: key }));
    const ui = { period: "week", date: null, detailed: false, pending: false, selected: null } as unknown as PeriodUi;
    const chart = periodBars(document, "en", "Chart", "kWh", bars, ui, "hint");
    document.body.append(chart);
    const plot = chart.querySelector<HTMLElement>(`.${VISUAL_CLASSES.historyBars}`)!;
    plot.querySelectorAll("button").forEach((button, index) => {
      button.getBoundingClientRect = () => ({ left: index * 20, right: index * 20 + 18, top: 0, bottom: 100, width: 18, height: 100, x: index * 20, y: 0, toJSON: () => ({}) });
    });
    plot.setPointerCapture = () => undefined;
    plot.releasePointerCapture = () => undefined;
    const readout = chart.querySelector(`.${VISUAL_CLASSES.historyReadout}`)!;
    expect(readout.textContent).toBe("hint");
    send(plot, "pointerdown", 5, 50);
    send(plot, "pointermove", 25, 50);
    expect(readout.textContent).toBe("bar b");
    send(plot, "pointermove", 65, 50);
    expect(readout.textContent).toBe("bar d");
    expect(ui.selected).toBeNull();
    send(plot, "pointerup", 65, 50);
    expect(ui.selected).toBe("d");
    chart.remove();
  });
});
