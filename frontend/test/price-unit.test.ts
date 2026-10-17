// The price unit preference: per-kWh prices in the market's minor unit (the default, as before) or its major
// unit, in the chart's header, Max and Min, price axis and readout, remembered in this browser only.

import { afterEach, describe, expect, it, vi } from "vitest";

import { createCardView, readoutTextFor, type CardView, type CardViewInput } from "../src/card-view";
import { choosePriceUnit, priceUnitNow } from "../src/card";
import { PRICE_UNIT_KEY, readPriceUnit, writePriceUnit, type PreferenceStore } from "../src/chart-preference";
import { priceAxisLabel, pricePerKwh, type FormatContext, type PriceUnit } from "../src/format";
import { dayFigures } from "../src/history";
import { translate, type Language } from "../src/i18n";
import { buildModel, type CardModel } from "../src/model";
import { decodeDashboard } from "../src/validate";
import { VISUAL_CLASSES as C } from "../src/visual-styles";
import { quarterHourRows, rawDashboard } from "./dashboard-fixtures";
import { mountPoint } from "./visual-fixtures";

const NOW_MS = Date.parse("2026-10-11T16:20:00+02:00");

/** One day in hours: 163,1 at 16:00 (now), 208,5 the highest, 92,1 the lowest. */
function hourPrices(): number[] {
  return Array.from({ length: 24 }, (_unused, hour) => (hour === 16 ? 163.1 : hour === 18 ? 208.5 : hour === 3 ? 92.1 : 120));
}

const EUR_MARKET = { area_id: "FI", area_name: "Finland", countries: ["FI"], currency: "EUR", major_unit: "€", minor_unit: "cent" };

function model(
  options: { unit?: PriceUnit; language?: Language; market?: Record<string, unknown> } = {},
): CardModel {
  const base = rawDashboard();
  const rows = quarterHourRows("2026-10-11T00:00:00+02:00", hourPrices(), { duration: 60, day: "2026-10-11" });
  const payload = rawDashboard({
    market: { ...(base.market as Record<string, unknown>), ...(options.market ?? {}) },
    prices: { ...(base.prices as Record<string, unknown>), intervals: rows, interval_count: rows.length, priced_slots: rows.length },
  });
  const decoded = decodeDashboard(payload);
  if (!decoded.ok) {
    throw new Error(`price unit scene did not decode: ${decoded.failure}`);
  }
  return buildModel({
    dashboard: decoded.value,
    language: options.language ?? "sv",
    nowMs: NOW_MS,
    chartAxis: "day",
    ...(options.unit === undefined ? {} : { priceUnit: options.unit }),
  });
}

function view(scene: CardModel, overrides: Partial<CardViewInput> = {}): { card: CardView; root: ShadowRoot } {
  const mount = mountPoint("unit");
  const card = createCardView({
    model: scene,
    mount: mount.root,
    idPrefix: "unit",
    now: () => NOW_MS,
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

function shown(root: ShadowRoot) {
  const text = (selector: string): string => root.querySelector(`.${selector}`)?.textContent?.trim() ?? "";
  return {
    max: text(C.summaryMax),
    min: text(C.summaryMin),
    current: text(C.summaryCurrent),
    axis: Array.from(root.querySelectorAll(`.${C.axisLabel}`)).map((node) => node.textContent),
  };
}

function nowMark(scene: CardModel) {
  const mark = scene.chart.marks.find((entry) => entry.startMs <= NOW_MS && NOW_MS < entry.endMs);
  if (mark === undefined) {
    throw new Error("no current mark");
  }
  return mark;
}

afterEach(() => {
  document.body.replaceChildren();
  window.localStorage.removeItem(PRICE_UNIT_KEY);
  vi.restoreAllMocks();
});

describe("prices in the minor unit, the default", () => {
  it("read as before: a tenth of an öre, whole öre on the axis", () => {
    const scene = model();
    expect(scene.priceUnit).toBe("minor");
    const { card, root } = view(scene);
    expect(shown(root)).toEqual({ max: "▲ 208,5", min: "▼ 92,1", current: "Nu 163,1 öre/kWh", axis: ["0", "104", "209"] });
    expect(readoutTextFor(scene, nowMark(scene))).toContain("163,1 öre/kWh");
    expect(root.querySelector(`.${C.summaryMax}`)?.getAttribute("aria-label")).toContain("208,5 öre/kWh");
    card.destroy();
  });
});

describe("prices in the major unit", () => {
  it("show kronor to two decimals in the header, Max and Min, the axis and the readout", () => {
    const scene = model({ unit: "major" });
    const { card, root } = view(scene);
    expect(shown(root)).toEqual({ max: "▲ 2,09", min: "▼ 0,92", current: "Nu 1,63 kr/kWh", axis: ["0", "1,0", "2,1"] });
    expect(root.querySelector(`.${C.summaryMax}`)?.getAttribute("aria-label")).toContain("2,09 kr/kWh");
    expect(readoutTextFor(scene, nowMark(scene))).toContain("1,63 kr/kWh");
    card.destroy();
  });

  it("show cent and euro for a euro market", () => {
    const minor = model({ language: "en", market: EUR_MARKET });
    expect(minor.summary.current).toBe("163.1 cent/kWh");
    const major = model({ language: "en", market: EUR_MARKET, unit: "major" });
    expect(major.summary.current).toBe("1.63 €/kWh");
    expect(readoutTextFor(major, nowMark(major))).toContain("1.63 €/kWh");
  });

  it("round the axis to the decimals its span needs", () => {
    const context: FormatContext = { language: "sv", timeZone: "", unit: "öre", currency: "SEK", majorUnit: "kr", priceUnit: "major" };
    expect(priceAxisLabel(context, 1520, 1520)).toBe("15");
    expect(priceAxisLabel(context, 260, 520)).toBe("2,6");
    expect(priceAxisLabel(context, 45, 90)).toBe("0,45");
    expect(priceAxisLabel(context, 0, 90)).toBe("0");
    expect(priceAxisLabel({ ...context, priceUnit: "minor" }, 104.25, 208.5)).toBe("104");
  });

  it("keep the minor unit where the market names no major one, and leave money totals alone", () => {
    const context: FormatContext = { language: "sv", timeZone: "", unit: "öre", currency: "SEK", majorUnit: null, priceUnit: "major" };
    expect(pricePerKwh(context, 163.1)).toBe("163,1 öre/kWh");
    const scene = model({ unit: "major" });
    expect(scene.figures.cost).toBe(model().figures.cost);
  });

  it("show the history's average price in the chosen unit", () => {
    const day = {
      period: "2026-10-11",
      sessions: 1,
      energy_kwh: 12.5,
      cost: 20.4,
      savings: null,
      solar_share: null,
      estimated: false,
      currency: "SEK",
      major_unit: "kr",
      minor_unit: "öre",
      average_price_minor_per_kwh: 163.1,
    } as unknown as Parameters<typeof dayFigures>[1];
    expect(dayFigures("sv", day)).toContain("163,1 öre/kWh");
    expect(dayFigures("sv", day, "major")).toContain("1,63 kr/kWh");
    expect(dayFigures("sv", day, "major")).toContain("20,4 kr");
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

describe("the price unit preference", () => {
  it("is the minor unit when nothing, or nothing usable, is stored", () => {
    const store = new MemoryStore();
    expect(readPriceUnit(() => store)).toBe("minor");
    store.setItem(PRICE_UNIT_KEY, "pounds");
    expect(readPriceUnit(() => store)).toBe("minor");
  });

  it("is the minor unit when storage is unavailable or throws, and a write then reports it was not kept", () => {
    const throwing: PreferenceStore = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(readPriceUnit(() => null)).toBe("minor");
    expect(readPriceUnit(() => throwing)).toBe("minor");
    expect(writePriceUnit(() => throwing, "major")).toBe(false);
    expect(writePriceUnit(() => null, "major")).toBe(false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(priceUnitNow()).toBe("minor");
  });

  it("round-trips through storage under one browser-wide key, apart from the axis", () => {
    const store = new MemoryStore();
    expect(writePriceUnit(() => store, "major")).toBe(true);
    expect(PRICE_UNIT_KEY).toBe("spotnav-card.price-unit");
    expect(store.values.get(PRICE_UNIT_KEY)).toBe("major");
    expect(readPriceUnit(() => store)).toBe("major");
  });

  it("is chosen once for every card on the page and announced to them", () => {
    let heard = 0;
    const listener = (): void => {
      heard += 1;
    };
    window.addEventListener("spotnav-card-price-unit", listener);
    choosePriceUnit("major");
    expect(priceUnitNow()).toBe("major");
    expect(window.localStorage.getItem(PRICE_UNIT_KEY)).toBe("major");
    expect(heard).toBe(1);
    choosePriceUnit("minor");
    expect(priceUnitNow()).toBe("minor");
    window.removeEventListener("spotnav-card-price-unit", listener);
  });
});

describe("the price unit in Display", () => {
  async function choose(root: ShadowRoot, value: string): Promise<void> {
    const option = root.querySelector<HTMLInputElement>(`[data-value-option='${value}']`)!;
    option.checked = true;
    option.dispatchEvent(new Event("change"));
    option.closest("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
  }

  it("names the market's own units, offers them to anyone and reports the choice", async () => {
    const chosen: string[] = [];
    const { card, root } = view(model(), { isAdmin: false, onPriceUnitChange: (unit) => chosen.push(unit) });
    card.openSettingsOverview();
    const row = root.querySelector<HTMLButtonElement>("[data-section='display'] [data-edit='price_unit']");
    expect(row?.textContent).toBe("öre/kWh");
    expect(root.querySelector("[data-section='display']")?.textContent).toContain(translate("sv", "display.priceUnit"));
    row!.click();
    const labels = Array.from(root.querySelectorAll<HTMLInputElement>("[data-value-option]")).map((radio) =>
      radio.closest("label")?.textContent,
    );
    expect(labels).toEqual(["öre/kWh", "kr/kWh"]);
    expect(root.textContent).toContain("Till exempel 1,63 kr/kWh");
    await choose(root, "major");
    expect(chosen).toEqual(["major"]);
    expect(root.querySelector("[data-section='display'] [data-edit='price_unit']")?.textContent).toBe("kr/kWh");
    card.destroy();
  });

  it("names cent and € for a euro market", () => {
    const { card, root } = view(model({ language: "en", market: EUR_MARKET }));
    card.openSettingsOverview();
    root.querySelector<HTMLButtonElement>("[data-section='display'] [data-edit='price_unit']")!.click();
    const labels = Array.from(root.querySelectorAll<HTMLInputElement>("[data-value-option]")).map((radio) =>
      radio.closest("label")?.textContent,
    );
    expect(labels).toEqual(["cent/kWh", "€/kWh"]);
    card.destroy();
  });

  it("is not offered where the market names no minor unit", () => {
    const { card, root } = view(model({ market: { minor_unit: null } }));
    card.openSettingsOverview();
    expect(root.querySelector("[data-section='display'] [data-edit='chart_axis']")).not.toBeNull();
    expect(root.querySelector("[data-edit='price_unit']")).toBeNull();
    card.destroy();
  });
});
