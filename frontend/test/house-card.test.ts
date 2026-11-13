// The house card (SpotNav 3.0 preview): the decoder on the backend's own fixtures, the body's chart, lines and
// details, and the element asking `spotnav/get_house` for its site.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { HOUSE_CARD_ELEMENT, parseHouseConfig, SpotnavHouseCard } from "../src/house-card";
import { houseChartHeight, renderHouseChart } from "../src/house-chart";
import { decodeHouse, type HouseAnswer } from "../src/house-model";
import { amount, hourFigures, houseBody, reviewLine, savingLine, type HouseState, type HouseUi } from "../src/house-view";
import { apiFailure, FakeHass } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function read(path: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, path), "utf8")) as Record<string, unknown>;
}

function answer(path = "house/villa_battery_sun.json"): HouseAnswer {
  const decoded = decodeHouse(read(path));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

/** The fixture's own moment: 13 November 2026, 17:20 in Stockholm. */
const NOW = Date.parse("2026-11-13T16:20:00+00:00");

function body(value: HouseAnswer, ui: Partial<HouseUi> = {}, language: (typeof LANGUAGES)[number] = "en", handlers = { onDetails: vi.fn(), onSelect: vi.fn() }) {
  const state: HouseState = { kind: "ready", answer: value };
  return houseBody({
    doc: document,
    language,
    state,
    ui: { detailsOpen: false, selected: null, ...ui },
    handlers,
    width: 340,
    nowMs: NOW,
    idPrefix: "house-test",
  });
}

describe("decoding the backend's own answer", () => {
  it("reads a site with the sun and a battery, and a house alone still learning", () => {
    const full = answer();
    expect(full.hours).toHaveLength(36);
    expect(full.battery?.mode).toBe("balanced");
    expect(full.battery?.review?.days).toBe(7);
    expect(full.days).toHaveLength(7);
    const learning = answer("house/house_learning.json");
    expect(learning.sun).toBeNull();
    expect(learning.battery).toBeNull();
    expect(learning.house.review).toBeNull();
  });

  it("refuses another version, and a missing or mistyped field, as a whole", () => {
    const raw = read("house/villa_battery_sun.json");
    expect(decodeHouse({ ...raw, api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    expect(decodeHouse({ ...raw, api_version: "1" })).toEqual({ ok: false, failure: "malformed" });
    const { house: _house, ...missing } = raw;
    expect(decodeHouse(missing)).toEqual({ ok: false, failure: "malformed" });
    const hours = (raw["hours"] as Array<Record<string, unknown>>).map((hour, index) => (index === 3 ? { ...hour, house_kw: "1.2" } : hour));
    expect(decodeHouse({ ...raw, hours })).toEqual({ ok: false, failure: "malformed" });
  });

  it("takes an added field and an action it does not know without failing", () => {
    const raw = read("house/villa_battery_sun.json");
    const hours = (raw["hours"] as Array<Record<string, unknown>>).map((hour, index) =>
      index === 20 ? { ...hour, battery_action: "discharge_to_car", heat_pump_kw: 1.1 } : hour,
    );
    const decoded = decodeHouse({ ...raw, hours, later: true });
    expect(decoded.ok).toBe(true);
    expect(decoded.ok && decoded.value.hours[20]!.battery_action).toBeNull();
  });
});

describe("the lines", () => {
  it("say the reviewed money side by side, in the area's major unit", () => {
    const value = answer();
    expect(savingLine("en", value)).toBe("Last 7 days: SpotNav's plan 51 kr · the battery itself 48 kr, against plain self-consumption");
    expect(savingLine("sv", value)).toBe("Senaste 7 dygnen: SpotNavs plan 51 kr · batteriet självt 48 kr, mot ren egenförbrukning");
    expect(amount("sv", 812.4, value.money)).toBe("8,12 kr");
  });

  it("before a day was reviewed, say what the plan expects; without a battery, say nothing", () => {
    const value = answer();
    const waiting: HouseAnswer = { ...value, battery: { ...value.battery!, review: null } };
    expect(savingLine("sv", waiting)).toBe(
      "Planen räknar med 8,12 kr bättre än ren egenförbrukning till lör 14 23:00. Första dygnets jämförelse kommer natten efter.",
    );
    expect(savingLine("en", { ...waiting, battery: { ...waiting.battery!, status: "no_plan", reason: "no_prices" } })).toBe(
      translate("en", "house.saving.noPlan"),
    );
    expect(savingLine("en", answer("house/house_learning.json"))).toBeNull();
  });

  it("say each forecast's error over the last week, or that the first review is to come", () => {
    expect(reviewLine("sv", answer())).toBe("Prognosens fel, senaste veckan: huset ±0,09 kW · sol ±0,07 kWh (korrigerad ±0,02 kWh) · batteri ±3 %");
    expect(reviewLine("en", answer("house/house_learning.json"))).toBe(translate("en", "house.review.learning"));
  });

  it("name an hour's figures: measured beside the forecast in the past, expected and planned ahead", () => {
    const value = answer();
    const past = value.hours[2]!;
    const ahead = value.hours.find((hour) => hour.battery_action === "grid_charge")!;
    expect(hourFigures("en", value, past, NOW)).toMatch(/^07:00–08:00 · house 1\.9 kW \(forecast 1\.7 kW\) · battery \d+ %$/);
    expect(hourFigures("en", value, ahead, NOW)).toMatch(/^0[2-4]:00–0[3-5]:00 · house 1\.0 kW expected · battery \d+ % planned · charges from the grid$/);
  });
});

describe("the body", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("draws one chart: a bar per hour, the first forecast over each measured one, the sun, the battery and the grid charging", () => {
    const card = body(answer());
    const svg = card.querySelector("svg")!;
    expect(svg.getAttribute("role")).toBe("img");
    expect(svg.querySelectorAll(".spotnav-house-bar")).toHaveLength(36);
    expect(svg.querySelectorAll(".spotnav-house-bar-forecast")).toHaveLength(24);
    expect(svg.querySelectorAll(".spotnav-house-forecast-tick")).toHaveLength(12);
    expect(svg.querySelector(".spotnav-house-sun-area")).not.toBeNull();
    expect(svg.querySelectorAll(".spotnav-house-soc")).toHaveLength(2);
    expect(svg.querySelectorAll(".spotnav-house-band[data-action='grid_charge']")).toHaveLength(3);
    expect(svg.querySelector(".spotnav-now")?.getAttribute("display")).toBeNull();
    expect(card.querySelector("[data-line='saving']")?.textContent).toContain("51 kr");
    expect(card.querySelector("[data-line='review']")?.textContent).toContain("±0.09 kW");
    expect(card.querySelector(".spotnav-house-details")).toBeNull();
  });

  it("keeps the chart calm on a house alone: no sun, no battery, no legend for them", () => {
    const card = body(answer("house/house_learning.json"));
    expect(card.querySelector(".spotnav-house-sun-area")).toBeNull();
    expect(card.querySelector(".spotnav-house-soc")).toBeNull();
    expect(card.querySelector("[data-line='saving']")).toBeNull();
    expect(Array.from(card.querySelectorAll(".spotnav-house-legend li")).map((item) => item.textContent)).toEqual(["House"]);
  });

  it("opens the details on a tap: the modes side by side, the days and the sources", () => {
    const handlers = { onDetails: vi.fn(), onSelect: vi.fn() };
    const closed = body(answer(), {}, "en", handlers);
    const toggle = closed.querySelector<HTMLButtonElement>("button[aria-expanded]")!;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    toggle.click();
    expect(handlers.onDetails).toHaveBeenCalledWith(true);
    const open = body(answer(), { detailsOpen: true }, "sv");
    const tables = open.querySelectorAll("table");
    expect(tables).toHaveLength(2);
    const modes = Array.from(tables[0]!.querySelectorAll("tbody tr")).map((row) => Array.from(row.children).map((cell) => cell.textContent));
    expect(modes).toEqual([
      ["Egenförbrukning", "0,00 kr", "–"],
      ["Balanserad (gäller)", "8,12 kr", "51 kr"],
      ["Max vinst", "9,05 kr", "51 kr"],
      ["Batteriets egen optimerare", "–", "48 kr"],
    ]);
    expect(tables[1]!.querySelectorAll("tbody tr")).toHaveLength(7);
    const sources = Array.from(open.querySelectorAll(".spotnav-house-facts li")).map((item) => item.textContent);
    expect(sources).toContain("Utetemperatur: sensor.nibe_bt1_outdoor_temperature");
    expect(sources).toContain("Solprognos: Forecast.Solar");
    expect(open.textContent).toContain("Batteriet styrs av sin egen optimerare");
    expect(open.textContent).toContain("Antaget hittills: batteriets lägsta nivå.");
  });

  it("walks the hours with the keyboard and shows each in the readout", () => {
    const handlers = { onDetails: vi.fn(), onSelect: vi.fn() };
    const card = body(answer(), {}, "en", handlers);
    document.body.append(card);
    const surface = card.querySelector<HTMLElement>(".spotnav-house-surface")!;
    const readout = card.querySelector(".spotnav-readout")!;
    expect(readout.textContent).toBe(translate("en", "house.hint"));
    surface.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    // From the running hour on.
    expect(handlers.onSelect).toHaveBeenLastCalledWith(12);
    expect(readout.textContent).toMatch(/^17:00–18:00 · house/);
    surface.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(readout.textContent).toBe(translate("en", "house.hint"));
  });

  it("is worded in every language", () => {
    for (const language of LANGUAGES) {
      const card = body(answer(), { detailsOpen: true }, language);
      expect(card.textContent, language).not.toMatch(/house\.|\{\w+\}/);
    }
  });

  it("says the loading, the failure and the missing target as one quiet sentence", () => {
    for (const [state, key] of [
      [{ kind: "loading" }, "house.loading"],
      [{ kind: "unconfigured" }, "house.unconfigured"],
      [{ kind: "failed", sentenceKey: "house.unsupported", code: "unknown_command" }, "house.unsupported"],
    ] as const) {
      const card = houseBody({
        doc: document,
        language: "en",
        state,
        ui: { detailsOpen: false, selected: null },
        handlers: { onDetails: vi.fn(), onSelect: vi.fn() },
        width: 340,
        nowMs: NOW,
        idPrefix: "x",
      });
      expect(card.querySelector("[role='status']")?.textContent).toBe(translate("en", key));
    }
  });
});

describe("the chart's geometry", () => {
  it("is as tall as the width asks, bounded, and maps a point to its hour", () => {
    expect(houseChartHeight(340)).toBe(150);
    expect(houseChartHeight(500)).toBe(220);
    expect(houseChartHeight(2000)).toBe(230);
    const value = answer();
    const chart = renderHouseChart({
      hours: value.hours,
      timeZone: value.time_zone,
      nowMs: NOW,
      width: 340,
      height: 150,
      labels: { kw: String, hour: () => "", day: () => "" },
      title: "t",
      description: "d",
      ids: { title: "a", description: "b" },
    });
    expect(chart.hourAt(chart.scale.left + 0.1)).toBe(0);
    expect(chart.hourAt(chart.scale.right - 0.1)).toBe(35);
    expect(chart.hourAt(0)).toBeNull();
    chart.select(5);
    expect(chart.element.querySelector(".spotnav-selection")?.getAttribute("display")).toBeNull();
  });
});

describe("the element", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
    if (customElements.get(HOUSE_CARD_ELEMENT) === undefined) {
      customElements.define(HOUSE_CARD_ELEMENT, SpotnavHouseCard);
    }
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function mount(config: unknown, hass: FakeHass): SpotnavHouseCard {
    const element = document.createElement(HOUSE_CARD_ELEMENT) as SpotnavHouseCard;
    element.setConfig(config);
    element.hass = hass.snapshot("s", "en");
    document.body.append(element);
    return element;
  }

  it("takes a site or a charger, and refuses a mistyped one", () => {
    expect(parseHouseConfig({ type: "custom:spotnav-house-card", site: "s1" })).toEqual({ type: "custom:spotnav-house-card", site: "s1" });
    expect(parseHouseConfig({ type: "custom:spotnav-house-card", charger: "c1", site: "" })).toEqual({
      type: "custom:spotnav-house-card",
      charger: "c1",
    });
    expect(() => parseHouseConfig({ type: "x", site: 3 })).toThrow();
    expect(() => parseHouseConfig(null)).toThrow();
  });

  it("asks for its site's house and shows it; a later failed read keeps it", async () => {
    const hass = new FakeHass();
    const element = mount({ type: "custom:spotnav-house-card", charger: "entry_a" }, hass);
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages).toEqual([{ type: "spotnav/get_house", api_version: 1, charger_id: "entry_a" }]);
    expect(element.shadowRoot!.textContent).toContain(translate("en", "house.loading"));
    hass.resolveNext(read("house/villa_battery_sun.json"));
    await vi.advanceTimersByTimeAsync(0);
    expect(element.shadowRoot!.querySelector("svg")).not.toBeNull();
    await vi.advanceTimersByTimeAsync(300_000);
    expect(hass.messages).toHaveLength(2);
    hass.rejectNext(apiFailure("spotnav_no_site"));
    await vi.advanceTimersByTimeAsync(0);
    expect(element.shadowRoot!.querySelector("svg")).not.toBeNull();
    element.remove();
    await vi.advanceTimersByTimeAsync(600_000);
    expect(hass.messages).toHaveLength(2);
  });

  it("says a SpotNav without the command needs updating", async () => {
    const hass = new FakeHass();
    const element = mount({ type: "custom:spotnav-house-card", site: "site_1" }, hass);
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages[0]).toEqual({ type: "spotnav/get_house", api_version: 1, site_id: "site_1" });
    hass.rejectNext(apiFailure("unknown_command"));
    await vi.advanceTimersByTimeAsync(0);
    expect(element.shadowRoot!.textContent).toContain(translate("en", "house.unsupported"));
  });

  it("asks nothing without a target", async () => {
    const hass = new FakeHass();
    const element = mount({ type: "custom:spotnav-house-card" }, hass);
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages).toEqual([]);
    expect(element.shadowRoot!.textContent).toContain(translate("en", "house.unconfigured"));
  });
});
