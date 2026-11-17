// The sun in the charger card's price chart: the dashboard's `sun` block, the series built from it on both axes,
// the layer drawn behind the price marks, the readout and legend, the Display choice, and the browser-only memory.

import { afterEach, describe, expect, it, vi } from "vitest";

import { chooseChartSun, chartSunNow } from "../src/card";
import { createCardView, readoutTextFor, type CardView, type CardViewInput } from "../src/card-view";
import { CHART_SUN_KEY, readChartSun, writeChartSun, type PreferenceStore } from "../src/chart-preference";
import { renderChart } from "../src/chart-render";
import { sunAt, sunRuns, sunSeries } from "../src/chart-sun";
import { translate } from "../src/i18n";
import { buildModel, type CardModel } from "../src/model";
import { decodeDashboard, sunOrNull } from "../src/validate";
import { VISUAL_CLASSES as C } from "../src/visual-styles";
import { quarterHourRows, rawDashboard } from "./dashboard-fixtures";
import { mountPoint, nowFor, TEST_LABELS } from "./visual-fixtures";

const HOUR_MS = 3_600_000;
const NOW_MS = Date.parse("2026-10-11T12:20:00+02:00");
const LANGUAGES = ["sv", "en", "de", "da", "nb", "fi", "nl", "fr", "es"] as const;

/** An hour of sun from the local hour of 11 October (or 12 October with `day`). */
function hour(local: number, kw: number, measured: boolean, day = 11): { start: string; kw: number; measured: boolean } {
  const stamp = `2026-10-${String(day).padStart(2, "0")}T${String(local).padStart(2, "0")}:00:00+02:00`;
  return { start: stamp, kw, measured };
}

const SUN_HOURS = [
  hour(8, 0.4, true),
  hour(9, 1.1, true),
  hour(10, 2.4, true),
  hour(11, 3.0, true),
  hour(12, 3.6, false),
  hour(13, 4.2, false),
  hour(14, 3.1, false),
  hour(8, 0.9, false, 12),
  hour(9, 2.2, false, 12),
];

function prices(): number[] {
  return Array.from({ length: 24 }, (_unused, index) => 100 + index);
}

function model(
  options: { sun?: unknown; axis?: "day" | "from_now"; chartSun?: boolean; tomorrow?: boolean; language?: CardModel["language"] } = {},
): CardModel {
  const base = rawDashboard();
  const today = quarterHourRows("2026-10-11T00:00:00+02:00", prices(), { duration: 60, day: "2026-10-11" });
  const tomorrow = quarterHourRows("2026-10-12T00:00:00+02:00", prices(), { duration: 60, day: "2026-10-12" });
  const rows = options.tomorrow === false ? today : [...today, ...tomorrow];
  const payload = rawDashboard({
    prices: { ...(base.prices as Record<string, unknown>), intervals: rows, interval_count: rows.length, priced_slots: rows.length },
    ...(options.sun === undefined ? {} : { sun: options.sun }),
  });
  const decoded = decodeDashboard(payload);
  if (!decoded.ok) {
    throw new Error(`sun scene did not decode: ${decoded.failure}`);
  }
  return buildModel({
    dashboard: decoded.value,
    language: options.language ?? "sv",
    nowMs: NOW_MS,
    chartAxis: options.axis ?? "day",
    ...(options.chartSun === undefined ? {} : { chartSun: options.chartSun }),
  });
}

function view(scene: CardModel, overrides: Partial<CardViewInput> = {}): { card: CardView; root: ShadowRoot } {
  const mount = mountPoint("sun");
  const card = createCardView({
    model: scene,
    mount: mount.root,
    idPrefix: "sun",
    now: () => NOW_MS,
    measure: () => ({ width: 360, height: 160 }),
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

afterEach(() => {
  document.body.replaceChildren();
  window.localStorage.removeItem(CHART_SUN_KEY);
  vi.restoreAllMocks();
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

describe("the dashboard's sun block", () => {
  it("is read hour by hour in time order, with the measured flag", () => {
    const block = sunOrNull({ hours: [hour(12, 3.6, false), hour(8, 0.4, true)] });
    expect(block?.hours.map((entry) => [entry.startMs, entry.kw, entry.measured])).toEqual([
      [Date.parse("2026-10-11T08:00:00+02:00"), 0.4, true],
      [Date.parse("2026-10-11T12:00:00+02:00"), 3.6, false],
    ]);
  });

  it("is nothing where it is missing, malformed or carries a stray key", () => {
    expect(sunOrNull(undefined)).toBeNull();
    expect(sunOrNull({ hours: [{ start: "2026-10-11T08:00:00+02:00", kw: -1, measured: true }] })).toBeNull();
    expect(sunOrNull({ hours: [{ start: "2026-10-11T08:00:00+02:00", kw: 1, measured: true, extra: 1 }] })).toBeNull();
    expect(sunOrNull({ hours: [], extra: true })).toBeNull();
    expect(sunOrNull({ hours: [], quarters: [{ start: "2026-10-11T08:00:00+02:00", kw: -1 }] })).toBeNull();
    expect(sunOrNull({ hours: [], quarters: [{ start: "2026-10-11T08:00:00+02:00", kw: 1, extra: 1 }] })).toBeNull();
    expect(sunOrNull({ hours: [], quarters: [{ start: "2026-10-11T08:00:00+02:00", kw: 1 }] })?.quarters).toHaveLength(1);
    expect(sunOrNull({ hours: [] })?.quarters).toEqual([]);
    expect(sunOrNull({ hours: [{ start: "soon", kw: 1, measured: true }] })).toBeNull();
  });

  it("is accepted on the dashboard, and a dashboard without it has none", () => {
    expect(model({ sun: { hours: SUN_HOURS } }).sunOffered).toBe(true);
    expect(model().sunOffered).toBe(false);
    expect(model().sun).toBeNull();
  });
});

describe("the sun series on the day axis", () => {
  it("keeps only today's hours, measured before now and forecast from it on", () => {
    const scene = model({ sun: { hours: SUN_HOURS } });
    expect(scene.sun?.points.map((point) => [point.kw, point.measured])).toEqual([
      [0.4, true],
      [1.1, true],
      [2.4, true],
      [3.0, true],
      [3.6, false],
      [4.2, false],
      [3.1, false],
    ]);
    // Each hour is drawn at its middle, 08:30 being 8.5 / 24 of the day.
    expect(scene.sun?.points[0]?.position).toBeCloseTo(8.5 / 24, 6);
    expect(scene.sun?.maxKw).toBe(4.2);
  });

  it("has its own floor so a grey day is not drawn as the whole plot", () => {
    const scene = model({ sun: { hours: [hour(12, 0.1, false)] } });
    expect(scene.sun?.maxKw).toBe(0.5);
  });
});

describe("the sun series on the from-now axis", () => {
  it("shows the hours of both days from the current hour, at their real time", () => {
    const scene = model({ sun: { hours: SUN_HOURS }, axis: "from_now" });
    expect(scene.chart.time.kind).toBe("range");
    const points = scene.sun?.points ?? [];
    // The axis starts at 12:00, so the earlier hours are off it and tomorrow's two are on.
    expect(points.map((point) => point.startMs)).toEqual(
      [hour(12, 0, false), hour(13, 0, false), hour(14, 0, false), hour(8, 0, false, 12), hour(9, 0, false, 12)].map((entry) =>
        Date.parse(entry.start),
      ),
    );
    const span = scene.chart.time.spanMs;
    expect(points[0]?.position).toBeCloseTo(0.5 * HOUR_MS / span, 6);
    expect(points.every((point) => point.position >= 0 && point.position <= 1)).toBe(true);
  });

  it("falls back to the day axis where no day ahead is published", () => {
    const scene = model({ sun: { hours: SUN_HOURS }, axis: "from_now", tomorrow: false });
    expect(scene.chart.time.kind).toBe("wall_clock");
    expect(scene.sun?.points).toHaveLength(7);
  });
});

describe("runs and lookup", () => {
  it("breaks a run at a missing hour", () => {
    const scene = model({ sun: { hours: [hour(8, 1, true), hour(9, 1, true), hour(11, 1, false), hour(12, 2, false)] } });
    expect(sunRuns(scene.sun!.points).map((run) => run.length)).toEqual([2, 2]);
  });

  it("joins the measured hours and the forecast from the running hour into one run", () => {
    const scene = model({ sun: { hours: [hour(10, 2.4, true), hour(11, 3.0, true), hour(12, 3.6, false), hour(13, 4.2, false)] } });
    expect(sunRuns(scene.sun!.points).map((run) => run.length)).toEqual([4]);
  });

  it("draws measured quarters up to now in place of the hours they cover, and the forecast on from there", () => {
    const quarter = (minutes: number, kw: number): { start: string; kw: number } => ({
      start: `2026-10-11T11:${String(minutes).padStart(2, "0")}:00+02:00`,
      kw,
    });
    const scene = model({
      sun: {
        hours: [hour(10, 2.4, true), hour(11, 9.9, false), hour(12, 3.6, false)],
        quarters: [quarter(0, 2.8), quarter(15, 3.0), quarter(30, 3.2)],
      },
    });
    const points = scene.sun!.points;
    // The hourly measured 10:00 stays (no quarters in it); the running hour's forecast gives way to the quarters.
    expect(points.map((point) => [point.endMs - point.startMs, point.measured])).toEqual([
      [HOUR_MS, true],
      [900_000, true],
      [900_000, true],
      [900_000, true],
      [HOUR_MS, false],
    ]);
    expect(sunRuns(points).map((run) => run.length)).toEqual([5]);
    const { element } = renderChart({
      series: scene.chart,
      bands: scene.bands,
      now: nowFor(scene),
      width: 360,
      height: 160,
      selected: null,
      title: "t",
      description: "d",
      labels: TEST_LABELS,
      sun: scene.sun,
      ids: { title: "s-title", description: "s-desc", hatch: "s-hatch" },
    });
    expect(element.querySelectorAll(`.${C.sunArea}`)).toHaveLength(1);
    expect(element.querySelectorAll(`.${C.sunLine}.${C.sunForecast}`)).toHaveLength(1);
  });

  it("finds the hour under an instant", () => {
    const scene = model({ sun: { hours: SUN_HOURS } });
    expect(sunAt(scene.sun, Date.parse("2026-10-11T12:45:00+02:00"))?.kw).toBe(3.6);
    expect(sunAt(scene.sun, Date.parse("2026-10-11T19:00:00+02:00"))).toBeNull();
    expect(sunAt(null, NOW_MS)).toBeNull();
  });

  it("builds no series from no block", () => {
    expect(sunSeries(null, model().chart)).toBeNull();
  });
});

describe("the sun layer", () => {
  function draw(scene: CardModel) {
    return renderChart({
      series: scene.chart,
      bands: scene.bands,
      now: nowFor(scene),
      width: 360,
      height: 160,
      selected: null,
      title: "t",
      description: "d",
      labels: TEST_LABELS,
      sun: scene.sun,
      ids: { title: "s-title", description: "s-desc", hatch: "s-hatch" },
    });
  }

  it("draws one area, a solid measured line and a dashed forecast line behind the price marks", () => {
    const scene = model({ sun: { hours: SUN_HOURS } });
    const { element } = draw(scene);
    const layer = element.querySelector(`.${C.sun}`)!;
    expect(layer.querySelectorAll(`.${C.sunArea}`)).toHaveLength(1);
    expect(layer.querySelectorAll(`.${C.sunLine}[data-kind='measured']`)).toHaveLength(1);
    expect(layer.querySelectorAll(`.${C.sunLine}.${C.sunForecast}`)).toHaveLength(1);
    const order = Array.from(element.children);
    expect(order.indexOf(layer)).toBeLessThan(order.indexOf(element.querySelector(`.${C.marks}`)!));
  });

  it("stays on its own scale: the price geometry is the same with and without it", () => {
    const withSun = draw(model({ sun: { hours: SUN_HOURS } }));
    const without = draw(model({ sun: { hours: SUN_HOURS }, chartSun: false }));
    expect(without.element.querySelector(`.${C.sun}`)).toBeNull();
    expect(withSun.targets.map((target) => [target.x, target.y])).toEqual(without.targets.map((target) => [target.x, target.y]));
    expect(withSun.scale).toEqual(without.scale);
  });

  it("never rises above the plot, the tallest hour reaching 80 % of its height", () => {
    const scene = model({ sun: { hours: SUN_HOURS } });
    const { element, scale } = draw(scene);
    const line = element.querySelector(`.${C.sunLine}[data-kind='forecast']`)!.getAttribute("d")!;
    const ys = Array.from(line.matchAll(/[ML][\d.]+ ([\d.]+)/g)).map((match) => Number(match[1]));
    expect(Math.min(...ys)).toBeCloseTo(scale.bottom - (scale.bottom - scale.top) * 0.8, 1);
    expect(Math.max(...ys)).toBeLessThanOrEqual(scale.bottom);
  });

  it("draws an all-forecast day as one dashed line only", () => {
    const { element } = draw(model({ sun: { hours: [hour(13, 2, false), hour(14, 3, false)] } }));
    expect(element.querySelectorAll(`.${C.sunLine}[data-kind='measured']`)).toHaveLength(0);
    expect(element.querySelectorAll(`.${C.sunForecast}`)).toHaveLength(1);
  });

  it("is drawn in the card, with its swatch and word, 360 px wide", () => {
    const { card, root } = view(model({ sun: { hours: SUN_HOURS } }));
    expect(root.querySelector(`.${C.sun}`)).not.toBeNull();
    expect(root.querySelector(`.${C.summarySun}`)?.textContent).toBe(translate("sv", "graph.sun.short"));
    expect(root.querySelector(`.${C.legend}`)?.textContent).toContain(translate("sv", "graph.legend.sun"));
    card.destroy();
  });

  it("is drawn from the measured hours alone where the site has no forecast, with the measured legend", () => {
    const measuredOnly = SUN_HOURS.slice(0, 4);
    const { card, root } = view(model({ sun: { hours: measuredOnly, quarters: [] } }));
    expect(root.querySelector(`.${C.sun}`)).not.toBeNull();
    expect(root.querySelector(`.${C.sunForecast}`)).toBeNull();
    const legend = root.querySelector(`.${C.legend}`)?.textContent ?? "";
    expect(legend).toContain(translate("sv", "graph.legend.sunMeasured"));
    expect(legend).not.toContain(translate("sv", "graph.legend.sun"));
    card.destroy();
  });

  it("names the grey tomorrow marks in a visible key line, and only when tomorrow is drawn", () => {
    const withTomorrow = view(model());
    const key = withTomorrow.root.querySelector<HTMLElement>("[data-part='chart-key']");
    expect(key?.hidden).toBe(false);
    expect(key?.textContent).toContain(translate("sv", "graph.key.tomorrow"));
    withTomorrow.card.destroy();
    const today = view(model({ tomorrow: false }));
    expect(today.root.querySelector<HTMLElement>("[data-part='chart-key']")?.hidden).toBe(true);
    today.card.destroy();
  });

  it("is offered but draws nothing where a capable site has no measured hour yet", () => {
    const scene = model({ sun: { hours: [], quarters: [] } });
    expect(scene.sunOffered).toBe(true);
    expect(scene.sun).toBeNull();
  });

  it("is not drawn, and has no swatch, when switched off or when the site has none", () => {
    for (const scene of [model({ sun: { hours: SUN_HOURS }, chartSun: false }), model()]) {
      const { card, root } = view(scene);
      expect(root.querySelector(`.${C.sun}`)).toBeNull();
      expect(root.querySelector(`.${C.summarySun}`)).toBeNull();
      expect(root.querySelector(`.${C.legend}`)?.textContent).not.toContain(translate("sv", "graph.legend.sun"));
      card.destroy();
    }
  });
});

describe("the readout", () => {
  function markAt(scene: CardModel, iso: string) {
    const ms = Date.parse(iso);
    return scene.chart.marks.find((entry) => entry.startMs <= ms && ms < entry.endMs)!;
  }

  it("adds the measured figure before now and the forecast after it", () => {
    const scene = model({ sun: { hours: SUN_HOURS } });
    expect(readoutTextFor(scene, markAt(scene, "2026-10-11T10:00:00+02:00"))).toContain("Sol 2,4 kW");
    expect(readoutTextFor(scene, markAt(scene, "2026-10-11T13:00:00+02:00"))).toContain("Sol, prognos 4,2 kW");
  });

  it("says nothing of the sun where there is none for the hour, or it is off", () => {
    const scene = model({ sun: { hours: SUN_HOURS } });
    expect(readoutTextFor(scene, markAt(scene, "2026-10-11T22:00:00+02:00"))).not.toContain("Sol");
    const off = model({ sun: { hours: SUN_HOURS }, chartSun: false });
    expect(readoutTextFor(off, markAt(off, "2026-10-11T10:00:00+02:00"))).not.toContain("Sol");
  });
});

describe("the sun in Display", () => {
  async function choose(root: ShadowRoot, value: string): Promise<void> {
    const option = root.querySelector<HTMLInputElement>(`[data-value-option='${value}']`)!;
    option.checked = true;
    option.dispatchEvent(new Event("change"));
    option.closest("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
  }

  it("is offered to anyone where the site has a sun source, on by default, and reports the choice", async () => {
    const chosen: boolean[] = [];
    const { card, root } = view(model({ sun: { hours: SUN_HOURS } }), { isAdmin: false, onChartSunChange: (on) => chosen.push(on) });
    card.openSettingsOverview();
    const row = root.querySelector<HTMLButtonElement>("[data-section='display'] [data-edit='chart_sun']");
    expect(row?.textContent).toBe(translate("sv", "settings.value.on"));
    expect(root.querySelector("[data-section='display']")?.textContent).toContain(translate("sv", "display.sun"));
    row!.click();
    await choose(root, "off");
    expect(chosen).toEqual([false]);
    expect(root.querySelector("[data-section='display'] [data-edit='chart_sun']")?.textContent).toBe(
      translate("sv", "settings.value.off"),
    );
    card.destroy();
  });

  it("is not offered where the site has no sun source", () => {
    const { card, root } = view(model());
    card.openSettingsOverview();
    expect(root.querySelector("[data-section='display'] [data-edit='chart_axis']")).not.toBeNull();
    expect(root.querySelector("[data-edit='chart_sun']")).toBeNull();
    card.destroy();
  });

  it("is still offered, showing off, while it is switched off", () => {
    const { card, root } = view(model({ sun: { hours: SUN_HOURS }, chartSun: false }));
    card.openSettingsOverview();
    expect(root.querySelector("[data-edit='chart_sun']")?.textContent).toBe(translate("sv", "settings.value.off"));
    card.destroy();
  });

  it("has its words in every card language", () => {
    for (const language of LANGUAGES) {
      for (const key of ["display.sun", "display.sun.help", "graph.legend.sun", "graph.legend.sunMeasured", "graph.key.tomorrow", "graph.key.plan", "graph.sun.short", "graph.sun.peak", "graph.readout.sun", "graph.readout.sunForecast"] as const) {
        expect(translate(language, key).length, `${language} ${key}`).toBeGreaterThan(0);
      }
    }
  });
});

describe("the sun preference", () => {
  it("is on when nothing, or nothing but an off, is stored", () => {
    const store = new MemoryStore();
    expect(readChartSun(() => store)).toBe(true);
    store.setItem(CHART_SUN_KEY, "maybe");
    expect(readChartSun(() => store)).toBe(true);
    store.setItem(CHART_SUN_KEY, "off");
    expect(readChartSun(() => store)).toBe(false);
  });

  it("is on when storage is unavailable or throws, and a write then reports it was not kept", () => {
    const throwing: PreferenceStore = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(readChartSun(() => null)).toBe(true);
    expect(readChartSun(() => throwing)).toBe(true);
    expect(writeChartSun(() => throwing, false)).toBe(false);
    expect(writeChartSun(() => null, false)).toBe(false);
  });

  it("round-trips under one browser-wide key", () => {
    const store = new MemoryStore();
    expect(writeChartSun(() => store, false)).toBe(true);
    expect(CHART_SUN_KEY).toBe("spotnav-card.chart-sun");
    expect(store.values.get(CHART_SUN_KEY)).toBe("off");
    expect(readChartSun(() => store)).toBe(false);
    writeChartSun(() => store, true);
    expect(readChartSun(() => store)).toBe(true);
  });

  it("is chosen once for every card on the page and announced to them", () => {
    let heard = 0;
    const listener = (): void => {
      heard += 1;
    };
    window.addEventListener("spotnav-card-chart-sun", listener);
    expect(chartSunNow()).toBe(true);
    chooseChartSun(false);
    expect(chartSunNow()).toBe(false);
    expect(window.localStorage.getItem(CHART_SUN_KEY)).toBe("off");
    expect(heard).toBe(1);
    chooseChartSun(true);
    expect(chartSunNow()).toBe(true);
    window.removeEventListener("spotnav-card-chart-sun", listener);
  });
});


describe("the sun's scale against the site's peak", () => {
  const sunLine = (root: ParentNode): string => root.querySelector(`.${C.sunLine}`)?.getAttribute("d") ?? "";
  const topY = (path: string): number => Math.min(...[...path.matchAll(/[ML][\d.]+ ([\d.]+)/g)].map((m) => Number(m[1])));
  const rendered = (sun: unknown): { y: number; label: string | null; maxKw: number } => {
    const scene = model({ sun });
    const result = view(scene);
    const line = sunLine(result.root);
    const label = result.root.querySelector(`.${C.sunPeak}`)?.textContent ?? null;
    result.card.destroy();
    return { y: topY(line), label, maxKw: scene.sun?.maxKw ?? 0 };
  };

  it("is read from the block, and a non-positive or stray peak is refused", () => {
    expect(sunOrNull({ hours: [], peak_kw: 8.4 })?.peakKw).toBe(8.4);
    expect(sunOrNull({ hours: [] })?.peakKw).toBeNull();
    expect(sunOrNull({ hours: [], peak_kw: 0 })).toBeNull();
    expect(sunOrNull({ hours: [], peak_kw: "8" })).toBeNull();
  });

  it("draws a dull day low against the peak, higher without one", () => {
    const withPeak = rendered({ hours: SUN_HOURS, peak_kw: 10 });
    const without = rendered({ hours: SUN_HOURS });
    expect(withPeak.maxKw).toBe(10);
    expect(without.maxKw).toBe(4.2);
    // A larger y is lower on the chart.
    expect(withPeak.y).toBeGreaterThan(without.y);
    expect(without.label).toBeNull();
    expect(withPeak.label).toBe(translate("sv", "graph.sun.peak", { kw: "10" }));
  });

  it("still fits a record hour above the stated peak", () => {
    const scene = model({ sun: { hours: SUN_HOURS, peak_kw: 3 } });
    expect(scene.sun?.maxKw).toBe(4.2);
  });

  it("keeps today's behaviour with no peak", () => {
    expect(model({ sun: { hours: SUN_HOURS } }).sun?.peakKw).toBeNull();
  });
});
