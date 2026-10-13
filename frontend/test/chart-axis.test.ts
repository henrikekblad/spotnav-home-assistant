// The chart's time axis preference: "day" (the wall-clock overlay, unchanged) and "from_now" (real time
// from the current hour to the end of the last published day), the strip on the same axis, and the
// browser-only memory of the choice.

import { afterEach, describe, expect, it } from "vitest";

import { createCardView, type CardView, type CardViewInput } from "../src/card-view";
import { chooseChartAxis, chartAxisNow } from "../src/card";
import { CHART_AXIS_KEY, readChartAxis, writeChartAxis, type PreferenceStore } from "../src/chart-preference";
import { renderChart } from "../src/chart-render";
import { stripBars, stripNowPosition, stripTicks } from "../src/chart-strip";
import { translate } from "../src/i18n";
import { VISUAL_CLASSES as C } from "../src/visual-styles";
import type { CardModel } from "../src/model";
import { axisSceneModel, CROSS_AXIS_FROM_NOW as SCENE, mountPoint, nowFor, TEST_LABELS } from "./visual-fixtures";

const HOUR_MS = 3_600_000;

function view(model: CardModel, overrides: Partial<CardViewInput> = {}): { card: CardView; root: ShadowRoot } {
  const mount = mountPoint("axis");
  const card = createCardView({
    model,
    mount: mount.root,
    idPrefix: "axis",
    now: () => SCENE.nowMs,
    measure: () => ({ width: 640, height: 160 }),
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

function renderOf(model: ReturnType<typeof axisSceneModel>) {
  return renderChart({
    series: model.chart,
    bands: model.bands,
    now: nowFor(model),
    width: 640,
    height: 160,
    selected: null,
    title: "t",
    description: "d",
    labels: { ...TEST_LABELS, day: (instantMs) => `day${instantMs}` },
    ids: { title: "a-title", description: "a-desc", hatch: "a-hatch" },
  });
}

describe("the From now axis with tomorrow published", () => {
  const model = axisSceneModel({ axis: "from_now" });

  it("runs from the start of the current hour to the end of the last published day", () => {
    expect(model.chart.time.kind).toBe("range");
    expect(model.chart.time.overlay).toBe(false);
    expect(model.chart.time.startMs).toBe(SCENE.axisStartMs);
    expect(model.chart.time.endMs).toBe(SCENE.axisEndMs);
    expect(model.chart.time.spanMs).toBe(SCENE.spanMs);
  });

  it("starts at the hour, not the instant, while the clock is inside it", () => {
    const later = axisSceneModel({ axis: "from_now", nowMs: SCENE.nowMs + 40 * 60_000 });
    expect(later.chart.time.startMs).toBe(SCENE.axisStartMs);
    expect(later.chart.marks[0]?.startMs).toBe(SCENE.axisStartMs);
    expect(later.chart.current?.startMs).toBe(SCENE.nowMs + 30 * 60_000);
  });

  it("draws one band from 16:00 (clipped at the axis start) to 05:30 tomorrow, nothing at today's 00:00-05:30", () => {
    expect(model.bands.installed).toHaveLength(1);
    const band = model.bands.installed[0]!;
    expect(band.startMs).toBe(SCENE.band.startMs);
    expect(band.endMs).toBe(SCENE.band.endMs);
    expect(band.startPosition).toBeCloseTo(SCENE.band.startPosition, 12);
    expect(band.endPosition).toBeCloseTo(SCENE.band.endPosition, 12);
    expect(band.tomorrow).toBe(false);
    // The applied proposal is covered by the installed band, so no second band.
    expect(model.bands.proposal).toEqual([]);

    const drawn = renderOf(model);
    const rects = Array.from(drawn.element.querySelectorAll(`.${C.bandInstalled}`));
    expect(rects).toHaveLength(1);
    const rect = rects[0]!;
    expect(Number(rect.getAttribute("x"))).toBeCloseTo(drawn.scale.left, 6);
    const width = drawn.scale.right - drawn.scale.left;
    expect(Number(rect.getAttribute("width"))).toBeCloseTo(width * (13.5 / 32), 6);
    // Solid, not hatched: no pattern at all.
    expect(rect.classList.contains(C.bandTomorrow)).toBe(false);
    expect(rect.getAttribute("fill")).toBeNull();
    expect(drawn.element.querySelector("pattern")).toBeNull();
  });

  it("draws nothing that ended before the axis, and no mark subdued", () => {
    expect(model.chart.marks.every((mark) => mark.endMs > SCENE.axisStartMs)).toBe(true);
    expect(model.chart.marks).toHaveLength(32 * 4);
    expect(model.chart.marks.some((mark) => mark.tomorrow)).toBe(false);
    expect(model.chart.marks.some((mark) => mark.dayKey === SCENE.tomorrow)).toBe(true);
    const drawn = renderOf(model);
    expect(drawn.element.querySelectorAll(`.${C.tomorrow}`)).toHaveLength(0);
    // Every mark placed by instant: tomorrow's 00:00 is 8 of 32 hours in.
    const midnight = model.chart.marks.find((mark) => mark.startMs === SCENE.axisStartMs + 8 * HOUR_MS);
    expect(midnight?.position).toBeCloseTo(8 / 32, 12);
  });

  it("puts the now line at the left edge, on the current interval", () => {
    const drawn = renderOf(model);
    expect(model.chart.current?.position).toBe(SCENE.nowPosition);
    expect(Number(drawn.nowLine.getAttribute("x1"))).toBeCloseTo(drawn.scale.left, 6);
  });

  it("ticks every 6 hours by instant, with the day cue at midnight", () => {
    expect(model.chart.axis.map((tick) => tick.position)).toEqual(SCENE.ticks.map((tick) => tick.position));
    expect(model.chart.axis.map((tick) => tick.dayStart === true)).toEqual(SCENE.ticks.map((tick) => tick.dayStart));
    const drawn = renderOf(model);
    const dividers = drawn.element.querySelectorAll(`.${C.dayDivider}`);
    expect(dividers).toHaveLength(1);
    const width = drawn.scale.right - drawn.scale.left;
    expect(Number(dividers[0]?.getAttribute("x1"))).toBeCloseTo(drawn.scale.left + width * (8 / 32), 6);
    const labels = Array.from(drawn.element.querySelectorAll(`.${C.tickLabel}`)).map((node) => node.textContent);
    const midnightMs = SCENE.axisStartMs + 8 * HOUR_MS;
    expect(labels).toContain(`day${midnightMs}`);
    expect(drawn.element.querySelector(`.${C.dayLabel}`)?.textContent).toBe(`day${midnightMs}`);
  });

  it("covers the drawn range with Max and Min", () => {
    const prices = model.chart.marks.map((mark) => mark.price);
    expect(Number(model.summary.maxFigure?.replace(",", "."))).toBeCloseTo(Math.max(...prices), 1);
    const day = axisSceneModel({ axis: "day" });
    const today = day.chart.days.find((entry) => entry.role === "today");
    expect(day.summary.maxFigure).not.toBeNull();
    expect(Number(day.summary.maxFigure?.replace(",", "."))).toBeCloseTo(today?.max ?? Number.NaN, 1);
    expect(Number(model.summary.minFigure?.replace(",", "."))).toBeCloseTo(Math.min(...prices), 1);
  });

  it("keeps the waiting-proposal rule: the running window installed, the proposal after it", () => {
    const pending = axisSceneModel({ axis: "from_now", plan: "pending" });
    expect(pending.bands.installed.map((band) => [band.startMs, band.endMs])).toEqual([
      [SCENE.axisStartMs, SCENE.axisStartMs + 15 * 60_000],
    ]);
    expect(pending.bands.proposal.map((band) => [band.startMs, band.endMs])).toEqual([
      [SCENE.axisStartMs + 15 * 60_000, SCENE.planEndMs],
    ]);
    expect([...pending.bands.installed, ...pending.bands.proposal].some((band) => band.tomorrow)).toBe(false);
    const drawn = renderOf(pending);
    expect(drawn.element.querySelectorAll(`.${C.bandProposal}`)).toHaveLength(1);
    expect(drawn.element.querySelector("pattern")).toBeNull();
  });
});

describe("the Day axis", () => {
  it("is the wall-clock overlay, as before", () => {
    const model = axisSceneModel({ axis: "day" });
    expect(model.chart.time.kind).toBe("wall_clock");
    expect(model.chart.axis.map((tick) => tick.position)).toEqual([0, 0.25, 0.5, 0.75, 1]);
    // Today's 14:00-24:00 and tomorrow's 00:00-05:30, tomorrow's hatched.
    expect(model.bands.installed.map((band) => [band.startPosition * 24, band.endPosition * 24, band.tomorrow])).toEqual([
      [14, 24, false],
      [0, 5.5, true],
    ]);
    expect(model.chart.marks).toHaveLength(192);
  });
});

describe("From now without tomorrow", () => {
  it("draws exactly what Day draws", () => {
    const fromNow = axisSceneModel({ axis: "from_now", tomorrow: false });
    const day = axisSceneModel({ axis: "day", tomorrow: false });
    expect(fromNow.chart.time.kind).toBe("wall_clock");
    expect(fromNow.chart.marks).toEqual(day.chart.marks);
    expect(fromNow.chart.axis).toEqual(day.chart.axis);
    expect(fromNow.bands).toEqual(day.bands);
    expect(fromNow.summary).toEqual(day.summary);
    const a = renderOf(fromNow).element.outerHTML;
    const b = renderOf(day).element.outerHTML;
    expect(a).toBe(b);
  });
});

describe("the strip on the From now axis", () => {
  const model = axisSceneModel({ axis: "from_now" });

  it("places the bars, the now line and the hour labels on the same axis", () => {
    expect(stripBars(model.bands, SCENE.nowMs)).toEqual([
      { kind: "installed", start: 0, end: 13.5 / 32, past: false, tomorrow: false },
    ]);
    expect(stripNowPosition(nowFor(model), SCENE.nowMs + 5 * 60_000, model.chart.time.spanMs)).toBeCloseTo(5 / (32 * 60), 10);
    expect(stripTicks(model.chart, SCENE.timeZone)).toEqual([
      { position: 2 / 32, label: "18" },
      { position: 8 / 32, label: "00", dayStart: true },
      { position: 14 / 32, label: "06" },
      { position: 20 / 32, label: "12" },
      { position: 26 / 32, label: "18" },
      { position: 1, label: "24" },
    ]);
  });

  it("draws a divider at midnight in the collapsed card", () => {
    const { card, root } = view(model, { chartCollapsed: true });
    const dividers = root.querySelectorAll<HTMLElement>(`.${C.stripDivider}`);
    expect(dividers).toHaveLength(1);
    expect(dividers[0]?.style.left).toBe(`${(8 / 32) * 100}%`);
    const labels = Array.from(root.querySelectorAll<HTMLElement>(`.${C.stripLabel}`));
    expect(labels.map((label) => label.textContent)).toEqual(["18", "00", "06", "12", "18", "24"]);
    expect(labels[0]?.dataset["edge"]).toBeUndefined();
    expect(labels[5]?.dataset["edge"]).toBe("end");
    card.destroy();
  });

  it("keeps the Day strip's labels on its edges", () => {
    const { card, root } = view(axisSceneModel({ axis: "day" }), { chartCollapsed: true });
    const labels = Array.from(root.querySelectorAll<HTMLElement>(`.${C.stripLabel}`));
    expect(labels.map((label) => label.textContent)).toEqual(["00", "06", "12", "18", "24"]);
    expect(labels[0]?.dataset["edge"]).toBe("start");
    expect(root.querySelectorAll(`.${C.stripDivider}`)).toHaveLength(0);
    card.destroy();
  });
});

class MemoryStore implements PreferenceStore {
  readonly values = new Map<string, string>();
  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

describe("the axis preference", () => {
  afterEach(() => {
    window.localStorage.removeItem(CHART_AXIS_KEY);
  });

  it("is Day when nothing, or nothing usable, is stored", () => {
    const store = new MemoryStore();
    expect(readChartAxis(() => store)).toBe("day");
    store.setItem(CHART_AXIS_KEY, "sideways");
    expect(readChartAxis(() => store)).toBe("day");
  });

  it("is Day when storage is unavailable or throws, and a write then reports it was not kept", () => {
    expect(readChartAxis(() => null)).toBe("day");
    const throwing: PreferenceStore = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(readChartAxis(() => throwing)).toBe("day");
    expect(writeChartAxis(() => throwing, "from_now")).toBe(false);
    expect(writeChartAxis(() => null, "from_now")).toBe(false);
    expect(() => readChartAxis(() => {
      throw new Error("no storage");
    })).not.toThrow();
  });

  it("round-trips through storage under one browser-wide key", () => {
    const store = new MemoryStore();
    expect(writeChartAxis(() => store, "from_now")).toBe(true);
    expect(store.values.get(CHART_AXIS_KEY)).toBe("from_now");
    expect(readChartAxis(() => store)).toBe("from_now");
  });

  it("is chosen once for every card on the page and announced to them", () => {
    let heard = 0;
    const listener = (): void => {
      heard += 1;
    };
    window.addEventListener("spotnav-card-chart-axis", listener);
    chooseChartAxis("from_now");
    expect(chartAxisNow()).toBe("from_now");
    expect(window.localStorage.getItem(CHART_AXIS_KEY)).toBe("from_now");
    expect(heard).toBe(1);
    chooseChartAxis("day");
    expect(chartAxisNow()).toBe("day");
    window.removeEventListener("spotnav-card-chart-axis", listener);
  });
});

describe("the Display section of Settings", () => {
  afterEach(() => {
    document.body.replaceChildren();
  });

  it("offers the axis to anyone, marked as this device only, and reports the choice", async () => {
    const chosen: string[] = [];
    const { card, root } = view(axisSceneModel({ axis: "day", language: "sv" }), {
      isAdmin: false,
      onChartAxisChange: (mode) => chosen.push(mode),
    });
    card.openSettingsOverview();
    const section = root.querySelector<HTMLElement>("[data-section='display']");
    expect(section).not.toBeNull();
    expect(section?.textContent).toContain(translate("sv", "settings.section.display"));
    expect(section?.querySelector("[data-device-only]")?.textContent).toBe(translate("sv", "display.applies"));
    const value = section!.querySelector<HTMLButtonElement>("[data-edit='chart_axis']")!;
    expect(value.textContent).toBe("Dygn");
    value.click();
    const option = root.querySelector<HTMLInputElement>("[data-value-option='from_now']")!;
    expect(option).not.toBeNull();
    option.checked = true;
    option.dispatchEvent(new Event("change"));
    option.closest("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(chosen).toEqual(["from_now"]);
    const again = root.querySelector<HTMLElement>("[data-section='display'] [data-edit='chart_axis']");
    expect(again?.textContent).toBe("Från nu");
    card.destroy();
  });
});
