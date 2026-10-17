// The chart_detail card option: a chart drawn finer for a card shown enlarged (a wall screen's fullscreen
// view scaled up). Without it the chart is exactly as before.

import { afterEach, describe, expect, it } from "vitest";

import { createCardView, type CardView, type CardViewInput } from "../src/card-view";
import { chartHeightForWidth, renderChart } from "../src/chart-render";
import { chartScale, gutterLabels, niceStep } from "../src/chart";
import { priceAxisLabel } from "../src/format";
import type { CardModel } from "../src/model";
import { parseCardConfig, editorConfig } from "../src/view";
import { CHART_DENSITY, VISUAL_CLASSES as C } from "../src/visual-styles";
import { axisSceneModel, CROSS_AXIS_FROM_NOW as SCENE, mountPoint, nowFor, TEST_LABELS } from "./visual-fixtures";

function view(model: CardModel, overrides: Partial<CardViewInput> = {}): { card: CardView; root: ShadowRoot } {
  const mount = mountPoint("detail");
  const card = createCardView({
    model,
    mount: mount.root,
    idPrefix: "detail",
    now: () => SCENE.nowMs,
    measure: () => ({ width: 460, height: chartHeightForWidth(460, CHART_DENSITY.detail) }),
    observe: () => null,
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
  return { card, root: mount.root };
}

function drawn(model: CardModel, detail: boolean, width = 460) {
  return renderChart({
    series: model.chart,
    bands: model.bands,
    now: nowFor(model),
    width,
    height: chartHeightForWidth(width, detail ? CHART_DENSITY.detail : CHART_DENSITY.normal),
    selected: null,
    title: "t",
    description: "d",
    labels: {
      ...TEST_LABELS,
      day: (instantMs) => `day${instantMs}`,
      hour: (instantMs) => `h${instantMs}`,
    },
    ...(detail ? { density: CHART_DENSITY.detail } : {}),
    ids: { title: "d-title", description: "d-desc", hatch: "d-hatch" },
  });
}

const texts = (root: Element, className: string): string[] =>
  Array.from(root.querySelectorAll(`.${className}`)).map((node) => node.textContent ?? "");

afterEach(() => {
  document.body.replaceChildren();
});

describe("the chart_detail option in the card's YAML", () => {
  it("is accepted as true or false and kept by the editor; anything else is refused", () => {
    expect(parseCardConfig({ type: "custom:spotnav-card", charger: "a", chart_detail: true })).toEqual({
      type: "custom:spotnav-card",
      charger: "a",
      chart_detail: true,
    });
    expect(parseCardConfig({ type: "custom:spotnav-card", charger: "a" })).not.toHaveProperty("chart_detail");
    expect(() => parseCardConfig({ type: "custom:spotnav-card", chart_detail: "yes" })).toThrow(/chart_detail/);
    expect(editorConfig("b", { chart: "compact", chart_detail: true })).toEqual({
      type: "custom:spotnav-card",
      charger: "b",
      chart: "compact",
      chart_detail: true,
    });
  });
});

describe("the chart without chart_detail", () => {
  it("is drawn as before: six-hour labels, bottom, middle and top prices, no density mark", () => {
    const model = axisSceneModel({ axis: "day" });
    const result = drawn(model, false);
    expect(result.element.hasAttribute("data-density")).toBe(false);
    expect(texts(result.element, C.tickLabel)).toHaveLength(5);
    expect(texts(result.element, C.axisLabel).length).toBeLessThanOrEqual(3);
    expect(result.scale.priceStep).toBeUndefined();
    expect(chartHeightForWidth(460)).toBe(120);
  });
});

describe("the chart with chart_detail", () => {
  it("labels all 24 hours on the Day axis", () => {
    const model = axisSceneModel({ axis: "day" });
    const result = drawn(model, true);
    expect(result.element.getAttribute("data-density")).toBe("detail");
    const labels = texts(result.element, C.tickLabel);
    expect(labels).toHaveLength(24);
    expect(new Set(labels).size).toBe(24);
  });

  it("labels every hour of the From now range and names the new day under its midnight", () => {
    const model = axisSceneModel({ axis: "from_now" });
    const result = drawn(model, true);
    const hours = (SCENE.axisEndMs - SCENE.axisStartMs) / 3_600_000;
    const dayLabels = texts(result.element, C.dayLabel);
    expect(dayLabels).toHaveLength(1);
    expect(texts(result.element, C.tickLabel)).toHaveLength(hours + dayLabels.length);
    // The gridlines and the dashed day divider stay at the six-hour ticks.
    expect(result.element.querySelectorAll(`.${C.dayDivider}`)).toHaveLength(1);
  });

  it("shows at least five price steps, on nice values, in either unit", () => {
    const model = axisSceneModel({ axis: "day" });
    const result = drawn(model, true);
    expect(texts(result.element, C.axisLabel).length).toBeGreaterThanOrEqual(5);
    for (const [min, max] of [[0, 208.5], [-12, 95], [0, 12], [0, 1430]] as const) {
      const scale = chartScale(460, 156, [min, max], CHART_DENSITY.detail);
      for (const unit of ["minor", "major"] as const) {
        const format = { language: "sv" as const, priceUnit: unit, unit: "öre", majorUnit: "kr", currency: "SEK", timeZone: "Europe/Stockholm" };
        const labels = gutterLabels(scale, (value, span) => priceAxisLabel(format, value, span));
        expect(labels.length, `${min}..${max} ${unit}`).toBeGreaterThanOrEqual(5);
        expect(labels.length).toBeLessThanOrEqual(9);
      }
    }
    expect(niceStep(0, 208.5, 5)).toBe(50);
  });

  it("draws smaller marks, smaller axis text and a taller chart", () => {
    const model = axisSceneModel({ axis: "day" });
    const normal = drawn(model, false);
    const detail = drawn(model, true);
    const radius = (result: typeof normal) => Math.max(...result.targets.map((target) => target.radius));
    expect(radius(detail)).toBeLessThan(radius(normal));
    expect(detail.scale.axisTextSize).toBeLessThan(normal.scale.axisTextSize);
    expect(CHART_DENSITY.detail.axisFontPx).toBeLessThan(CHART_DENSITY.normal.axisFontPx);
    expect(CHART_DENSITY.detail.gridWidth).toBeLessThan(CHART_DENSITY.normal.gridWidth);
    expect(chartHeightForWidth(460, CHART_DENSITY.detail)).toBeGreaterThan(chartHeightForWidth(460));
  });

  it("reaches the card view's chart, and only the chart", () => {
    const model = axisSceneModel({ axis: "day" });
    const { card, root } = view(model, { chartDetail: true });
    const svg = root.querySelector("svg[data-density='detail']");
    expect(svg).not.toBeNull();
    expect(texts(svg as Element, C.tickLabel)).toHaveLength(24);
    card.destroy();
    const plain = view(model);
    expect(plain.root.querySelector("svg[data-density]")).toBeNull();
    plain.card.destroy();
  });
});
