// The card in a box of fixed height: the plan (pure) and the view applying it, against a simulated layout.

import { describe, expect, it } from "vitest";

import { planFill, type FillReading } from "../src/card-fill";
import { createCardView, type CardView, type CardViewInput } from "../src/card-view";
import type { ChartObserverLike } from "../src/chart-interaction";
import { chartHeightForWidth } from "../src/chart-render";
import type { CardModel } from "../src/model";
import { CHART_DENSITY, VISUAL_CLASSES as C, VISUAL_STYLES } from "../src/visual-styles";
import { baseModel, mountPoint } from "./visual-fixtures";

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

/**
 * A simulated layout: the card is `OTHERS` px plus the chart viewport plus the status sentence, which is
 * `statusLines` lines of `LINE` px in full, or the clamped count while clamped. The box is `box` when
 * the container has a height of its own, else the card's own height.
 */
function layout(options: { box: number | null; statusLines: number; others?: number }) {
  const others = options.others ?? 260;
  let readings = 0;
  const read = (card: HTMLElement): FillReading => {
    readings += 1;
    const viewport = card.querySelector<HTMLElement>(`.${C.viewport}`);
    const status = card.querySelector<HTMLElement>(`.${C.status}`);
    const clamped = status?.dataset["lines"];
    const statusHeight = (clamped === undefined ? options.statusLines : Number(clamped)) * LINE;
    const chart = viewport === null || viewport.hidden ? 0 : Number.parseFloat(viewport.style.height);
    const natural = others + chart + statusHeight;
    return { box: options.box ?? natural, natural, statusHeight, lineHeight: LINE };
  };
  return { read, readings: () => readings, others };
}

function fillView(
  read: (card: HTMLElement) => FillReading,
  overrides: Partial<CardViewInput> = {},
  model: CardModel = baseModel(),
): { view: CardView; root: ShadowRoot; fire: () => void; observed: () => Element | null } {
  const mount = mountPoint("fill");
  let onBox: (() => void) | null = null;
  let observedBox: Element | null = null;
  const view = createCardView({
    model,
    mount: mount.root,
    idPrefix: "fill",
    now: () => Date.parse("2026-09-22T04:30:00Z"),
    measure: () => ({ width: WIDTH, height: BASIS }),
    observe: () => null,
    observeBox: (_box, onResize): ChartObserverLike => {
      onBox = onResize;
      return {
        observe(element: Element) {
          observedBox = element;
        },
        disconnect() {
          onBox = null;
        },
      };
    },
    readFill: read,
    onAction: () => {},
    onOpenSettings: () => {},
    onSaveSettings: () => {},
    onReloadSettings: () => {},
    onReapplySettings: () => {},
    onOpenMarket: () => {},
    onSaveMarket: () => {},
    onReloadMarket: () => {},
    onReapplyMarket: () => {},
    onMarketAreaChange: () => {},
    isAdmin: true,
    onSelectStrategy: () => {},
    ...overrides,
  });
  return { view, root: mount.root, fire: () => onBox?.(), observed: () => observedBox };
}

function parts(root: ShadowRoot) {
  const card = root.querySelector<HTMLElement>(`.${C.card}`);
  const viewport = root.querySelector<HTMLElement>(`.${C.viewport}`);
  const status = root.querySelector<HTMLElement>(`.${C.status}`);
  const svg = () => root.querySelector<SVGSVGElement>(`.${C.viewport} svg`);
  return { card, viewport, status, svg };
}

describe("the card in a box", () => {
  it("observes the mount's host and changes nothing when the box is the card's own height", () => {
    const sim = layout({ box: null, statusLines: 2 });
    const { view, root, fire, observed } = fillView(sim.read);
    const { card, viewport, status, svg } = parts(root);
    expect(observed()).toBe(root.host);
    const before = svg();
    const html = card?.outerHTML;
    fire();
    expect(sim.readings()).toBe(1);
    expect(card?.outerHTML).toBe(html);
    expect(card?.hasAttribute("data-fill")).toBe(false);
    expect(status?.hasAttribute("title")).toBe(false);
    expect(viewport?.style.height).toBe(`${BASIS}px`);
    expect(svg()).toBe(before);
    expect(svg()?.getAttribute("viewBox")).toBe(`0 0 ${WIDTH} ${BASIS}`);
    view.destroy();
  });

  it("grows the chart in a taller box, with the action bar at the bottom edge", () => {
    const sim = layout({ box: 520, statusLines: 1 });
    const { view, root, fire } = fillView(sim.read);
    const { card, viewport, status, svg } = parts(root);
    const lowest = (): number =>
      Math.max(...Array.from(svg()?.querySelectorAll("line") ?? []).map((line) => Number(line.getAttribute("y2") ?? 0)));
    const autoBottom = lowest();
    fire();
    const height = 520 - sim.others - LINE;
    expect(card?.dataset["fill"]).toBe("true");
    expect(viewport?.style.height).toBe(`${height}px`);
    // The chart is drawn for the measured height: viewBox, the interaction's size and the plot's bottom.
    expect(svg()?.getAttribute("viewBox")).toBe(`0 0 ${WIDTH} ${height}`);
    expect(view.chart().size()).toEqual({ width: WIDTH, height });
    expect(lowest()).toBeGreaterThan(autoBottom);
    expect(lowest()).toBeLessThanOrEqual(height);
    expect(status?.hasAttribute("data-lines")).toBe(false);
    // Nothing left over: the content ends where the box does, so the bar sits on its bottom edge.
    expect(card?.style.getPropertyValue("--spotnav-fill-spare")).toBe("0px");
    view.destroy();
  });

  it("shrinks the chart to its minimum in a shorter box, then clamps the status with its full text as title", () => {
    const sim = layout({ box: 380, statusLines: 5 });
    const { view, root, fire } = fillView(sim.read);
    const { viewport, status, svg } = parts(root);
    fire();
    // Natural: 260 + 122 + 100 = 482; 380 with the chart at its minimum is 100 short: five lines, one kept.
    expect(status?.dataset["lines"]).toBe("1");
    expect(status?.style.getPropertyValue("--spotnav-status-lines")).toBe("1");
    expect(status?.title).toBe(status?.textContent);
    expect(viewport?.style.height).toBe(`${MIN}px`);
    expect(svg()?.getAttribute("viewBox")).toBe(`0 0 ${WIDTH} ${MIN}`);
    view.destroy();
  });

  it("keeps the lines that fit and lets the chart take what the clamp freed", () => {
    const sim = layout({ box: 450, statusLines: 5 });
    const { view, root, fire } = fillView(sim.read);
    const { viewport, status } = parts(root);
    fire();
    // 450 - 260 = 190 for chart and status: 120 for the chart leaves 70, three lines (60); the chart gets 130.
    expect(status?.dataset["lines"]).toBe("3");
    expect(viewport?.style.height).toBe("130px");
    view.destroy();
  });

  it("does not clamp a long status in a tall box", () => {
    const sim = layout({ box: 700, statusLines: 6 });
    const { view, root, fire } = fillView(sim.read);
    const { viewport, status } = parts(root);
    fire();
    expect(status?.hasAttribute("data-lines")).toBe(false);
    expect(status?.hasAttribute("title")).toBe(false);
    expect(viewport?.style.height).toBe(`${700 - 260 - 6 * LINE}px`);
    view.destroy();
  });

  it("settles in one pass: observing again draws nothing new and changes nothing", () => {
    for (const box of [300, 380, 450, 520, 800]) {
      const sim = layout({ box, statusLines: 4 });
      const { view, root, fire } = fillView(sim.read);
      const { card, svg } = parts(root);
      fire();
      const drawn = svg();
      const html = card?.outerHTML;
      for (let round = 0; round < 5; round += 1) {
        fire();
      }
      expect(svg()).toBe(drawn);
      expect(card?.outerHTML).toBe(html);
      view.destroy();
    }
  });

  it("follows the box both ways and returns to the ordinary card when the box goes", () => {
    let box: number | null = 520;
    const others = 260;
    const read = (card: HTMLElement): FillReading => {
      const viewport = card.querySelector<HTMLElement>(`.${C.viewport}`);
      const chart = Number.parseFloat(viewport?.style.height ?? "0");
      const natural = others + chart + 3 * LINE;
      return { box: box ?? natural, natural, statusHeight: 3 * LINE, lineHeight: LINE };
    };
    const { view, root, fire } = fillView(read);
    const { card, viewport, status, svg } = parts(root);
    fire();
    expect(viewport?.style.height).toBe(`${520 - others - 3 * LINE}px`);
    box = 400;
    fire();
    // 400 - 320 = 80 for the chart, 40 short of its minimum: two of the three lines go.
    expect(viewport?.style.height).toBe(`${MIN}px`);
    expect(status?.dataset["lines"]).toBe("1");
    box = null;
    fire();
    expect(card?.hasAttribute("data-fill")).toBe(false);
    expect(status?.hasAttribute("data-lines")).toBe(false);
    expect(status?.hasAttribute("title")).toBe(false);
    expect(viewport?.style.height).toBe(`${BASIS}px`);
    expect(svg()?.getAttribute("viewBox")).toBe(`0 0 ${WIDTH} ${BASIS}`);
    view.destroy();
  });

  it("does not move the chart when a point is selected (the readout's line is reserved)", () => {
    const sim = layout({ box: 520, statusLines: 1 });
    const { view, root, fire } = fillView(sim.read);
    fire();
    const drawn = parts(root).svg();
    view.chart().handleKeydown(new KeyboardEvent("keydown", { key: "ArrowRight" }));
    expect(view.readoutText()).not.toBe("");
    fire();
    expect(parts(root).svg()).toBe(drawn);
    view.destroy();
  });

  it("fits again when an action error appears above the chart", () => {
    let extra = 0;
    const read = (card: HTMLElement): FillReading => {
      const viewport = card.querySelector<HTMLElement>(`.${C.viewport}`);
      const natural = 260 + extra + Number.parseFloat(viewport?.style.height ?? "0") + LINE;
      return { box: 520, natural, statusHeight: LINE, lineHeight: LINE };
    };
    const { view, root, fire } = fillView(read);
    fire();
    const { viewport } = parts(root);
    expect(viewport?.style.height).toBe(`${520 - 260 - LINE}px`);
    extra = 40;
    view.setActionError({ sentenceKey: "history.failed", code: null });
    expect(viewport?.style.height).toBe(`${520 - 300 - LINE}px`);
    view.destroy();
  });

  it("disconnects the box observer on destroy", () => {
    const sim = layout({ box: 520, statusLines: 1 });
    const { view, fire } = fillView(sim.read);
    view.destroy();
    fire();
    expect(sim.readings()).toBe(0);
  });

  it("ships the clamp and the bottom-edge rules only for a filled card", () => {
    expect(VISUAL_STYLES).toMatch(/:host \{[^}]*height: 100%/s);
    expect(VISUAL_STYLES).toMatch(new RegExp(`\\.${C.card}\\[data-fill\\] \\.${C.status}\\[data-lines\\] \\{[^}]*-webkit-line-clamp: var\\(--spotnav-status-lines`, "s"));
    expect(VISUAL_STYLES).toMatch(new RegExp(`\\.${C.card}\\[data-fill\\] \\.${C.actionBarWrap} \\{[^}]*--spotnav-fill-spare`, "s"));
  });
});
