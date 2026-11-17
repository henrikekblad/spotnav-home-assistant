// The house card (SpotNav 3.0 preview): the decoder on the backend's own fixtures, the body's chart, lines and
// details, and the element asking `spotnav/get_house` for its site.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { HOUSE_CARD_ELEMENT, parseHouseConfig, SpotnavHouseCard } from "../src/house-card";
import { houseChartHeight, renderHouseChart } from "../src/house-chart";
import { blockHeights, flowHeight, flowLabels, MIN_BAND, MIN_NODE } from "../src/house-flow";
import { HOUSE_STYLES } from "../src/house-styles";
import { decodeHouse, type HouseAnswer } from "../src/house-model";
import { amount, hourFigures, houseBody, reviewLine, savingLine, todayDetail, todayLine, type HouseState, type HouseUi } from "../src/house-view";
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

/** The answer as from a backend before the perfect plan (or before its first day): the comparison in money. */
function withoutPerfect(value: HouseAnswer): HouseAnswer {
  const battery = value.battery === null ? null : { ...value.battery, review: value.battery.review === null ? null : { ...value.battery.review, perfect: null } };
  return { ...value, battery, day_review: value.day_review === null ? null : { ...value.day_review, perfect: null } };
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
    expect(savingLine("en", value)).toBe("Last 7 days: SpotNav's plan 51 kr · the battery itself 45 kr, against plain self-consumption");
    expect(savingLine("sv", value)).toBe("Senaste 7 dygnen: SpotNavs plan 51 kr · batteriet självt 45 kr, mot ren egenförbrukning");
    expect(amount("sv", 812.4, value.money)).toBe("8,1 kr");
  });

  it("before a day was reviewed, say what the plan expects; without a battery, say nothing", () => {
    const value = answer();
    const waiting: HouseAnswer = { ...value, battery: { ...value.battery!, review: null } };
    expect(savingLine("sv", waiting)).toBe(
      "Planen räknar med 8,1 kr bättre än ren egenförbrukning till lör 14 23:00. Första dygnets jämförelse kommer natten efter.",
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
    expect(hourFigures("en", value, past, NOW)).toMatch(/^07:00–08:00 · house 1\.9 kW \(forecast 1\.7 kW\) · heat 0\.9 kW · battery \d+ %$/);
    expect(hourFigures("en", value, ahead, NOW)).toMatch(
      /^0[2-4]:00–0[3-5]:00 · house 1\.0 kW expected · heat 0\.5 kW expected · battery \d+ % planned · charges from the grid$/,
    );
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
    // The heat pump's part of each bar: measured in the past, forecast ahead.
    expect(svg.querySelectorAll(".spotnav-house-heat-bar")).toHaveLength(36);
    expect(svg.querySelectorAll(".spotnav-house-heat-forecast")).toHaveLength(24);
    expect(svg.querySelector(".spotnav-house-sun-area")).not.toBeNull();
    // The battery's level has its own labelled axis in the price view only: never on the kW axis.
    expect(svg.querySelector(".spotnav-house-soc")).toBeNull();
    expect(card.querySelector("button[data-legend='battery']")).toBeNull();
    expect(body(answer(), { view: "price" }).querySelectorAll("svg .spotnav-house-soc")).toHaveLength(2);
    // The plan's tints belong to the price view only.
    expect(svg.querySelector(".spotnav-house-band")).toBeNull();
    expect(svg.querySelector(".spotnav-now")?.getAttribute("display")).toBeNull();
    expect(card.querySelector("[data-line='saving']")).toBeNull();
    expect(card.querySelector("[data-line='perfect-headline']")?.textContent).toBe("Sigenergy AI 79 % of perfect · SpotNav Balanced 85 %");
    // The forecasts' errors are in the details, not on the front.
    expect(card.querySelector("[data-line='review']")).toBeNull();
    expect(card.querySelector(".spotnav-house-details")).toBeNull();
  });

  it("keeps the chart calm on a house alone: no sun, no battery, no legend for them", () => {
    const card = body(answer("house/house_learning.json"));
    expect(card.querySelector(".spotnav-house-sun-area")).toBeNull();
    expect(card.querySelector(".spotnav-house-soc")).toBeNull();
    expect(card.querySelector("[data-line='saving']")).toBeNull();
    expect(Array.from(card.querySelectorAll(".spotnav-house-legend li")).map((item) => item.textContent)).toEqual(["House"]);
  });

  it("says what test means when its chip is tapped, in place or through the card", () => {
    const card = body(answer(), {}, "sv");
    const chip = card.querySelector<HTMLButtonElement>("button[data-action='test']")!;
    const explain = card.querySelector<HTMLElement>("[data-line='test-explain']")!;
    expect(chip.textContent).toBe("Test · styr inget än");
    expect(chip.getAttribute("aria-controls")).toBe(explain.id);
    expect(explain.hidden).toBe(true);
    chip.click();
    expect(chip.getAttribute("aria-expanded")).toBe("true");
    expect(explain.hidden).toBe(false);
    const onTest = vi.fn();
    body(answer(), {}, "sv", { onDetails: vi.fn(), onSelect: vi.fn(), onTest } as never).querySelector<HTMLButtonElement>("button[data-action='test']")!.click();
    expect(onTest).toHaveBeenCalledWith(true);
    expect(body(answer(), { testOpen: true }, "sv").querySelector<HTMLElement>("[data-line='test-explain']")!.hidden).toBe(false);
  });

  it("says Test only while SpotNav does not control the battery, and a quiet chip once it does", () => {
    for (const control of ["off", "shadow_only", "verifying", "waiting", "failed", null]) {
      const value = answer();
      value.battery = { ...value.battery!, control };
      const card = body(value, {}, "sv");
      expect(card.querySelector("button[data-action='test']"), String(control)).not.toBeNull();
      expect(card.querySelector("[data-line='controlling']")).toBeNull();
    }
    const on = answer();
    on.battery = { ...on.battery!, control: "on" };
    const card = body(on, { detailsOpen: true }, "sv");
    expect(card.querySelector("button[data-action='test']")).toBeNull();
    expect(card.querySelector("[data-line='test-explain']")).toBeNull();
    expect(card.querySelector("[data-line='controlling']")!.textContent).toBe("Styr batteriet");
    expect(card.textContent).toContain("SpotNav styr batteriet efter planen.");
    expect(card.textContent).not.toContain("SpotNav planerar batteriet men styr det inte än.");
  });

  it("opens the details on a tap: the modes side by side, the days and the sources", () => {
    const handlers = { onDetails: vi.fn(), onSelect: vi.fn() };
    const closed = body(answer(), {}, "en", handlers);
    const toggle = closed.querySelector<HTMLButtonElement>("button.spotnav-house-details-toggle")!;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    toggle.click();
    expect(handlers.onDetails).toHaveBeenCalledWith(true);
    const open = body(answer(), { detailsOpen: true }, "sv");
    const tables = open.querySelectorAll("table");
    // The modes in money, the shares in money, the battery's work, the days.
    expect(tables).toHaveLength(4);
    const modes = Array.from(tables[0]!.querySelectorAll("tbody tr")).map((row) => Array.from(row.children).map((cell) => cell.textContent));
    expect(modes).toEqual([
      ["Egenförbrukning", "0 kr", "–"],
      ["Balanserad (vald)", "8,1 kr", "51 kr"],
      ["Max vinst", "9,1 kr", "51 kr"],
      ["Batteriets egen optimerare", "–", "45 kr"],
    ]);
    expect(tables[3]!.querySelectorAll("tbody tr")).toHaveLength(7);
    expect(open.querySelector("[data-line='review']")?.textContent).toContain("±0,09 kW");
    const sources = Array.from(open.querySelectorAll(".spotnav-house-facts li")).map((item) => item.textContent);
    expect(sources).toContain("Utetemperatur: sensor.nibe_bt1_outdoor_temperature");
    expect(sources).toContain("Solprognos: Forecast.Solar");
    expect(open.textContent).toContain("Batteriet styrs av sin egen optimerare");
    expect(open.textContent).toContain("Antaget hittills: batteriets lägsta nivå.");
  });

  it("words every estimate code the battery's facts can carry, in every language", () => {
    const value = answer();
    const codes = ["capacity_from_ah", "rated_from_current", "capacity_learned", "rated_learned"];
    value.battery!.estimates = codes;
    for (const language of LANGUAGES) {
      const text = body(value, { detailsOpen: true }, language).textContent ?? "";
      for (const code of codes) {
        expect(translate(language, `house.estimate.${code}` as never)).not.toBe(`house.estimate.${code}`);
      }
      expect(text).toContain(translate(language, "house.estimate.capacity_learned" as never));
      expect(text).toContain(translate(language, "house.estimate.rated_learned" as never));
    }
    expect(body(value, { detailsOpen: true }, "en").textContent).toContain("the size (learnt from use)");
  });

  it("walks the hours with the keyboard and shows each in the readout", () => {
    const handlers = { onDetails: vi.fn(), onSelect: vi.fn() };
    const card = body(answer(), {}, "en", handlers);
    document.body.append(card);
    const surface = card.querySelector<HTMLElement>(".spotnav-house-surface")!;
    const readout = card.querySelector(".spotnav-readout")!;
    expect(readout.textContent).toBe(translate("en", "house.hint.energy"));
    surface.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    // From the running hour on.
    expect(handlers.onSelect).toHaveBeenLastCalledWith(12);
    expect(readout.textContent).toMatch(/^17:00–18:00 · house/);
    surface.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(readout.textContent).toBe(translate("en", "house.hint.energy"));
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

describe("the battery app's views, calm", () => {
  it("head the card with what the battery has earned, the battery app's measure, the cost under it and the mode beside the level", () => {
    expect(todayLine("sv", answer())).toBe("Batteriet har tjänat 11 kr i dag");
    expect(todayLine("sv", answer("house/house_learning.json"))).toBeNull();
    expect(todayDetail("sv", answer())).toBe("Elkostnad 38 kr");
    const value = answer();
    expect(todayDetail("sv", { ...value, today: { ...value.today!, sold: 420 } })).toBe("Elkostnad 38 kr · sålt solel 4,2 kr");
    const card = body(answer(), {}, "sv");
    expect(card.querySelector("[data-line='today']")?.textContent).toBe("Batteriet har tjänat 11 kr i dag");
    expect(card.querySelector("[data-line='today-hint']")?.textContent).toBe("Samma mått som batteriets app.");
    expect(card.querySelector("[data-line='today-detail']")?.textContent).toBe("Elkostnad 38 kr");
    expect(card.querySelector(".spotnav-house-sub")?.textContent).toBe("Batteri 48 % · läge Balanserad");
  });

  it("switch the chart between energy and price with the battery, the price view drawing both prices and no bars", () => {
    const onView = vi.fn();
    const energy = body(answer(), {}, "en", { onDetails: vi.fn(), onSelect: vi.fn(), onView } as never);
    const buttons = energy.querySelectorAll<HTMLButtonElement>("button[data-view]");
    expect(Array.from(buttons).map((button) => button.getAttribute("aria-pressed"))).toEqual(["true", "false"]);
    buttons[1]!.click();
    expect(onView).toHaveBeenCalledWith("price");
    const price = body(answer(), { view: "price" }, "en");
    const svg = price.querySelector("svg")!;
    expect(svg.querySelector(".spotnav-house-bar")).toBeNull();
    expect(svg.querySelector(".spotnav-house-price-buy")?.getAttribute("d")).toMatch(/^M/);
    expect(svg.querySelector(".spotnav-house-price-sell")).not.toBeNull();
    expect(Array.from(svg.querySelectorAll("text")).map((text) => text.textContent)).toEqual(expect.arrayContaining(["100 %", "Now"]));
    expect(svg.querySelectorAll(".spotnav-house-band[data-action='export_discharge']").length).toBeGreaterThan(0);
    expect(svg.querySelectorAll(".spotnav-house-band[data-action='grid_charge']")).toHaveLength(3);
    const surface = price.querySelector<HTMLElement>(".spotnav-house-surface")!;
    document.body.append(price);
    surface.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(price.querySelector(".spotnav-readout")?.textContent).toMatch(/^17:00–18:00 · buy 3\d\d öre\/kWh · sell \d+ öre\/kWh · battery/);
  });

  it("offer no price view where nothing is priced", () => {
    expect(body(answer("house/house_learning.json")).querySelector("button[data-view]")).toBeNull();
  });

  it("show today's energy from sources to uses in the details, and no month calendar (it is in the history)", () => {
    // Without the energy flow (an older backend) the details keep the two bars.
    const card = body({ ...answer(), energy_flow: null }, { detailsOpen: true }, "sv");
    const keys = Array.from(card.querySelectorAll(".spotnav-house-flow-key")).map((item) => item.textContent);
    expect(keys[0]).toMatch(/^Sol 4,5 kWh \(\d+ %\) · Batteri 9,8 kWh \(\d+ %\) · Nät 34,3 kWh \(\d+ %\)$/);
    expect(keys[1]).toMatch(/^Batteri 27,1 kWh/);
    expect(card.querySelector(".spotnav-house-calendar-day")).toBeNull();
    expect(card.textContent).toContain("Lagrat nu: 32,3 kWh à 121 öre/kWh i snitt");
  });
});

/** The legend's labels in order, as shown. */
function legendOf(card: HTMLElement): string[] {
  return Array.from(card.querySelectorAll(".spotnav-house-legend li")).map((item) => item.textContent ?? "");
}

/** The y of the top of the tallest house bar, to see the axis rescale. */
function tallestBar(card: HTMLElement): number {
  return Math.min(...Array.from(card.querySelectorAll(".spotnav-house-bar")).map((bar) => Number(bar.getAttribute("y"))));
}

describe("the legend", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("keeps the grid charging and selling tints and their legend items to the price view", () => {
    expect(legendOf(body(answer()))).toEqual(["House", "Heat", "Forecast was", "Sun", "Car"]);
    expect(legendOf(body(answer(), { view: "price" }))).toEqual([
      "Buy price",
      "Sell price",
      "Battery 0–100 % (dashed: plan)",
      "Charges from the grid",
      "Sells to the grid",
    ]);
    const price = body(answer(), { view: "price" });
    expect(price.querySelectorAll(".spotnav-house-band")).not.toHaveLength(0);
    expect(body(answer()).querySelector(".spotnav-house-band")).toBeNull();
  });

  it("gives every item as a button that tells whether its series is shown, and asks to toggle it", () => {
    const onLegend = vi.fn();
    const card = body(answer(), {}, "en", { onDetails: vi.fn(), onSelect: vi.fn(), onLegend } as never);
    const buttons = Array.from(card.querySelectorAll<HTMLButtonElement>(".spotnav-house-legend button"));
    expect(buttons.every((button) => button.type === "button" && button.hasAttribute("aria-pressed"))).toBe(true);
    const pressed = Object.fromEntries(buttons.map((button) => [button.dataset["legend"], button.getAttribute("aria-pressed")]));
    expect(pressed).toEqual({ house: "true", heat: "true", forecast: "true", sun: "true", car: "false" });
    card.querySelector<HTMLButtonElement>("button[data-legend='sun']")!.click();
    expect(onLegend).toHaveBeenLastCalledWith("energy", ["car", "sun"]);
    card.querySelector<HTMLButtonElement>("button[data-legend='car']")!.click();
    expect(onLegend).toHaveBeenLastCalledWith("energy", []);
  });

  it("leaves a hidden series out of the chart and the readout", () => {
    const value = answer();
    const hidden = { energy: ["car", "heat", "sun", "forecast", "battery"] };
    const card = body(value, { hidden });
    const svg = card.querySelector("svg")!;
    expect(svg.querySelector(".spotnav-house-heat-bar")).toBeNull();
    expect(svg.querySelector(".spotnav-house-sun-area")).toBeNull();
    expect(svg.querySelector(".spotnav-house-sun-raw")).toBeNull();
    expect(svg.querySelector(".spotnav-house-forecast-tick")).toBeNull();
    expect(svg.querySelector(".spotnav-house-soc")).toBeNull();
    expect(svg.querySelectorAll(".spotnav-house-bar")).toHaveLength(36);
    const past = value.hours[2]!;
    expect(hourFigures("en", value, past, NOW, "energy", new Set(hidden.energy))).toBe("07:00–08:00 · house 1.9 kW");
    const buttons = Array.from(card.querySelectorAll<HTMLButtonElement>(".spotnav-house-legend button"));
    expect(buttons.filter((button) => button.getAttribute("aria-pressed") === "false")).toHaveLength(4);
    // Hiding the house itself leaves the rest (the forecast ticks, the heat) standing.
    const noHouse = body(value, { hidden: { energy: ["house", "car"] } }).querySelector("svg")!;
    expect(noHouse.querySelector(".spotnav-house-bar")).toBeNull();
    expect(noHouse.querySelector(".spotnav-house-heat-bar")).not.toBeNull();
  });

  it("rescales the kW axis to the series shown", () => {
    const value = answer();
    const withCar = body(value, { hidden: { energy: [] } });
    const without = body(value);
    expect(tallestBar(withCar)).toBeGreaterThan(tallestBar(without));
    const axis = (card: HTMLElement): string[] => Array.from(card.querySelectorAll("svg .spotnav-axis-label, svg text")).map((text) => text.textContent ?? "");
    expect(axis(withCar).join()).not.toBe(axis(without).join());
    // Only the sun left: the axis shrinks to the sun's figures.
    const sunOnly = body(value, { hidden: { energy: ["car", "house", "heat", "forecast"] } });
    expect(axis(sunOnly).join()).not.toBe(axis(without).join());
    expect(sunOnly.querySelector("svg .spotnav-house-bar")).toBeNull();
  });

  it("draws the car as its own bar beside the house's, hidden until toggled on", () => {
    const value = answer();
    const carHours = value.hours.filter((hour) => hour.car_kw !== null && hour.car_kw > 0);
    expect(carHours.length).toBeGreaterThan(0);
    expect(body(value).querySelector("svg .spotnav-house-car-bar")).toBeNull();
    const shown = body(value, { hidden: { energy: [] } });
    const cars = Array.from(shown.querySelectorAll("svg .spotnav-house-car-bar"));
    expect(cars).toHaveLength(carHours.length);
    // Beside, not over: the car's bar starts where the house's of the same hour ends.
    const bars = Array.from(shown.querySelectorAll("svg .spotnav-house-bar"));
    const houseOfHour = bars.find((bar) => Math.abs(Number(bar.getAttribute("x")) - (Number(cars[0]!.getAttribute("x")) - 0)) < 40)!;
    expect(Number(houseOfHour.getAttribute("x")) + Number(houseOfHour.getAttribute("width"))).toBeLessThanOrEqual(Number(cars[0]!.getAttribute("x")));
    expect(shown.querySelector(".spotnav-readout")).not.toBeNull();
    const peak = carHours.find((hour) => hour.car_kw === Math.max(...carHours.map((h) => h.car_kw as number)))!;
    expect(hourFigures("en", value, peak, NOW, "energy", new Set())).toContain(`car ${peak.car_kw!.toFixed(1)} kW`);
    expect(hourFigures("en", value, peak, NOW)).not.toContain("car ");
  });

  it("hides the price lines and the tints from the price view's legend, and the battery's axis with the battery", () => {
    const hidden = { price: ["sellPrice", "charge", "battery"] };
    const svg = body(answer(), { view: "price", hidden }).querySelector("svg")!;
    expect(svg.querySelector(".spotnav-house-price-sell")).toBeNull();
    expect(svg.querySelector(".spotnav-house-price-buy")).not.toBeNull();
    expect(svg.querySelector(".spotnav-house-band[data-action='grid_charge']")).toBeNull();
    expect(svg.querySelector(".spotnav-house-band[data-action='export_discharge']")).not.toBeNull();
    expect(svg.querySelector(".spotnav-house-soc")).toBeNull();
    expect(Array.from(svg.querySelectorAll("text")).map((text) => text.textContent)).not.toContain("100 %");
    const value = answer();
    const hour = value.hours.find((h) => h.battery_action === "grid_charge")!;
    expect(hourFigures("en", value, hour, NOW, "price", new Set(hidden.price))).toMatch(/^[0-9:–]+ · buy \d+ öre\/kWh$/);
  });

  it("has a word for the car in every language", () => {
    for (const language of LANGUAGES) {
      expect(legendOf(body(answer(), {}, language)).length).toBeGreaterThan(4);
      expect(translate(language, "house.legend.car")).not.toBe("");
    }
    expect(translate("sv", "house.legend.car")).toBe("Bil");
  });
});

describe("the legend in the card element", () => {
  const store = new Map<string, string>();
  beforeEach(() => {
    store.clear();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("remembers the choice per view in this browser, the car hidden to begin with", async () => {
    const { readHiddenSeries, writeHiddenSeries } = await import("../src/house-series");
    expect(readHiddenSeries()).toEqual({});
    writeHiddenSeries("energy", ["sun"]);
    writeHiddenSeries("price", ["buy"]);
    expect(readHiddenSeries()).toEqual({ energy: ["sun"], price: ["buy"] });
    store.set("spotnav-card.house.hidden.energy", "not json");
    expect(readHiddenSeries()).toEqual({ price: ["buy"] });
  });

  it("renders from the defaults when storage is missing or throws", async () => {
    const { readHiddenSeries, writeHiddenSeries } = await import("../src/house-series");
    vi.stubGlobal("localStorage", undefined);
    expect(readHiddenSeries()).toEqual({});
    expect(() => writeHiddenSeries("energy", [])).not.toThrow();
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    });
    expect(readHiddenSeries()).toEqual({});
    expect(() => writeHiddenSeries("energy", [])).not.toThrow();
    expect(legendOf(body(answer(), { hidden: readHiddenSeries() })).length).toBe(5);
  });
});

describe("the modes side by side in the body", () => {
  it("show what the battery would have saved: SpotNav's modes and the battery as it ran, the chosen mode marked", () => {
    // From a backend before the perfect plan: the money against self-consumption.
    const value = withoutPerfect(answer());
    const card = body(value, {}, "sv");
    const part = card.querySelector("[data-part='modes']")!;
    expect(part.querySelector(".spotnav-house-compare-head")?.textContent).toBe("Jämfört med om batteriet bara lagrat egen sol, senaste 7 dygnen");
    const rows = Array.from(part.querySelectorAll<HTMLElement>(".spotnav-house-compare-row"));
    expect(rows.map((row) => row.dataset["row"])).toEqual(["balanced", "max_profit", "own"]);
    expect(rows.filter((row) => row.dataset["inForce"] === "true").map((row) => row.dataset["row"])).toEqual([value.battery!.mode]);
    expect(rows.find((row) => row.dataset["inForce"])?.textContent).toBe("SpotNav Balanserad vald+51 kr");
    // The battery runs Sigenergy's own optimiser: the row names it.
    expect(rows[2]!.textContent).toBe("Batteriets egen styrning (Sigenergy AI)+45 kr");
    const free = body({ ...value, battery: { ...value.battery!, management: "free" } }, {}, "sv");
    expect(free.querySelector("[data-part='modes'] [data-row='own']")?.textContent).toBe("Batteriets egen styrning+45 kr");
  });

  it("say plainly when a mode has no full day reviewed yet", () => {
    const value = answer();
    const battery = value.battery!;
    const modes = { ...battery.modes, max_profit: { planned_saving: 9, reviewed_saving: null, reviewed_days: 0 } };
    const card = body({ ...value, battery: { ...battery, modes, review: null } }, {}, "sv");
    const rows = Array.from(card.querySelectorAll<HTMLElement>(".spotnav-house-compare-row"));
    expect(rows.find((row) => row.dataset["row"] === "max_profit")?.textContent).toContain("inget fullt dygn");
    expect(rows.find((row) => row.dataset["row"] === "own")?.textContent).toContain("inget fullt dygn");
    // Before any review the line still says what the plan expects.
    expect(card.querySelector("[data-line='saving']")).not.toBeNull();
  });
});

describe("the shares of the perfect plan", () => {
  it("lead the comparison: the battery's own optimiser and the chosen mode, a bar a way, and what perfect is", () => {
    const card = body(answer(), {}, "sv");
    const part = card.querySelector<HTMLElement>("[data-part='modes']")!;
    expect(part.dataset["view"]).toBe("perfect");
    expect(part.querySelector(".spotnav-house-compare-head")?.textContent).toBe("Andel av perfekt, senaste 7 dygnen");
    expect(part.querySelector("[data-line='perfect-headline']")?.textContent).toBe("Sigenergy AI 79 % av perfekt · SpotNav Balanserad 85 %");
    const rows = Array.from(part.querySelectorAll<HTMLElement>(".spotnav-house-compare-row"));
    expect(rows.map((row) => row.dataset["row"])).toEqual(["own", "balanced", "max_profit", "self_consumption"]);
    expect(rows.map((row) => row.querySelector(".spotnav-house-compare-value")?.textContent)).toEqual(["79 %", "85 %", "78 %", "55 %"]);
    expect(rows[1]!.dataset["inForce"]).toBe("true");
    expect(rows[0]!.querySelector<HTMLElement>(".spotnav-house-compare-fill")!.style.width).toBe("79%");
    // Ways sold on these days: every share against the perfect plan that may sell, and the note says so.
    expect(part.querySelector("[data-line='perfect-explain']")?.textContent).toBe("Perfekt: bästa möjliga i efterhand, får sälja (Max vinst-regler).");
    // A week: the day boundary washes out, so no word about midnight; one day still on its own value says so.
    expect(part.querySelector("[data-line='perfect-midnight']")).toBeNull();
    expect(part.querySelector("[data-line='perfect-provisional']")?.textContent).toBe(
      "Det som fanns kvar vid midnatt räknas med dygnets eget pris för en av dygnen, tills nästa dygns priser finns.",
    );
    // No money on the front: it is in the details.
    expect(part.textContent).not.toContain("kr");
  });

  it("say on a single day that what was left at midnight counted", () => {
    const card = body(answer("house/villa_past_day.json"), {}, "sv");
    const part = card.querySelector("[data-part='modes']")!;
    expect(part.querySelector(".spotnav-house-compare-head")?.textContent).toMatch(/^Andel av perfekt, tors 12 nov/);
    expect(part.querySelector("[data-line='perfect-midnight']")?.textContent).toBe("Räknat med det som fanns kvar i batteriet vid midnatt.");
    expect(part.querySelector("[data-line='perfect-provisional']")?.textContent).toBe(
      "Det som fanns kvar vid midnatt räknas med dygnets eget pris tills nästa dygns priser finns.",
    );
  });

  it("name a share clamped to 0-100 % and a way with too small a difference to compare", () => {
  it("say which rules the perfect plan kept: may sell, does not sell, or a day stored before as the mode's", () => {
    const value = answer();
    const explain = (rules: string | null): string | null | undefined => {
      const perfect = { ...value.battery!.review!.perfect!, rules };
      const card = body({ ...value, battery: { ...value.battery!, review: { ...value.battery!.review!, perfect } } }, {}, "sv");
      return card.querySelector("[data-line='perfect-explain']")?.textContent;
    };
    expect(explain("max_profit")).toBe("Perfekt: bästa möjliga i efterhand, får sälja (Max vinst-regler).");
    expect(explain("no_sell")).toBe("Perfekt: bästa möjliga i efterhand, utan försäljning (inget gick ut på nätet).");
    expect(explain("mode")).toBe("Perfekt: bästa möjliga i efterhand, med samma regler.");
  });

    const value = answer();
    const perfect = value.battery!.review!.perfect!;
    const clamped = { ...perfect, clamped: ["own"], shares: { ...perfect.shares, own: 100, max_profit: null } };
    const card = body({ ...value, battery: { ...value.battery!, review: { ...value.battery!.review!, perfect: clamped } } }, {}, "sv");
    expect(card.querySelector("[data-line='perfect-clamped']")?.textContent).toBe("Sigenergy AI: utanför 0–100 %, visas vid närmaste gräns (en förlust som 0 %).");
    expect(card.querySelector("[data-row='max_profit'] .spotnav-house-compare-value")?.textContent).toBe("för liten skillnad att jämföra");
    // Not managed by its own optimiser: the battery's own control by name.
    const free = body({ ...value, battery: { ...value.battery!, management: "free" } }, {}, "sv");
    expect(free.querySelector("[data-line='perfect-headline']")?.textContent).toMatch(/^Batteriets egen styrning 79 % av perfekt/);
  });

  it("show the money behind the shares, the battery's work and the cells' temperature in the details", () => {
    const open = body(answer(), { detailsOpen: true }, "sv");
    const part = open.querySelector("[data-part='perfect-details']")!;
    const [money, work] = Array.from(part.querySelectorAll("table"));
    const rows = (table: Element) => Array.from(table.querySelectorAll("tbody tr")).map((row) => Array.from(row.children).map((cell) => cell.textContent));
    expect(rows(money!)).toEqual([
      ["Perfekt", "81 kr", "100 %"],
      ["Sigenergy AI", "64 kr", "79 %"],
      ["SpotNav Balanserad", "69 kr", "85 %"],
      ["SpotNav Max vinst", "63 kr", "78 %"],
      ["SpotNav Egenförbrukning", "45 kr", "55 %"],
    ]);
    expect(rows(work!)[0]).toEqual(["Sigenergy AI", "2,87", "28", "63,0", "2,8"]);
    expect(rows(work!).at(-1)).toEqual(["Perfekt", "3,13", "14", "98,0", "42,0"]);
    expect(part.querySelector("[data-line='cell-temperature']")?.textContent).toBe("Battericeller: medel 22,4 °C, högst 30,0 °C, 25 min över 30 °C (sex dygn).");
    // The rules behind the shares: the next day's mean price at midnight, and 5 kr a day for a share.
    expect(part.textContent).toContain("nästa dygns medelpris för köpt el");
    expect(part.textContent).toContain("minst 5 kr per dygn");
  });

  it("say a day under 90 % measured is incomplete and leave such days out of a week", () => {
    const value = answer("house/villa_past_day.json");
    const day = value.day_review!.perfect!;
    const none = { own: null, self_consumption: null, balanced: null, max_profit: null };
    const partial = { ...day, days: 0, partial: 1, saving: null, shares: none, savings: none, provisional: 0 };
    const card = body({ ...value, day_review: { ...value.day_review!, perfect: partial } }, {}, "sv");
    const part = card.querySelector("[data-part='modes']")!;
    expect(part.querySelector("[data-line='perfect-partial']")?.textContent).toBe("Ofullständigt dygn: under 90 % av dygnet mättes, så det jämförs inte.");
    expect(part.querySelectorAll(".spotnav-house-compare-row")).toHaveLength(0);
    expect(part.querySelector("[data-line='perfect-headline']")).toBeNull();
    // A week with two incomplete days says they are left out.
    const week = answer();
    const perfect = { ...week.battery!.review!.perfect!, partial: 2 };
    const weekCard = body({ ...week, battery: { ...week.battery!, review: { ...week.battery!.review!, perfect } } }, {}, "sv");
    expect(weekCard.querySelector("[data-line='perfect-partial']")?.textContent).toBe("2 ofullständiga dygn räknas inte med.");
  });

  it("are worded in every language and read from an older backend as none", () => {
    for (const language of LANGUAGES) {
      for (const key of ["perfect.partialDay", "perfect.partial.one", "perfect.partial.other", "perfect.partialOnly.one", "perfect.partialOnly.other", "hist.house.share.partial", "hist.house.share.small", "perfect.heading.one", "perfect.heading.other", "perfect.headingDay", "perfect.headingPeriod", "perfect.headline", "perfect.headlineOne", "perfect.explain", "perfect.explainSell", "perfect.explainNoSell", "perfect.midnight", "perfect.provisional.one", "perfect.provisional.other", "perfect.provisionalDay", "perfect.clamped", "perfect.nothing", "perfect.money.heading", "perfect.money.saved", "perfect.money.share", "perfect.money.perfect", "perfect.money.note", "perfect.work.heading", "perfect.work.way", "perfect.work.cycles", "perfect.work.switches", "perfect.work.fromGrid", "perfect.work.toGrid", "perfect.work.note", "perfect.temperature.one", "perfect.temperature.other", "hist.house.col.share"] as const) {
        expect(translate(language, key as never, {}), `${language} ${key}`).not.toBe(key);
      }
    }
    const raw = JSON.parse(JSON.stringify(read("house/villa_past_day.json")));
    delete raw.day_review.perfect;
    delete raw.battery.review.perfect;
    const decoded = decodeHouse(raw);
    expect(decoded.ok && decoded.value.day_review?.perfect === null && decoded.value.battery?.review?.perfect === null).toBe(true);
    const broken = JSON.parse(JSON.stringify(read("house/villa_past_day.json")));
    delete broken.day_review.perfect.shares.own;
    expect(decodeHouse(broken).ok).toBe(false);
  });
});

describe("the day in the battery app's terms", () => {
  const handlers = { onDetails: vi.fn(), onSelect: vi.fn(), } as never;

  it("shows the three figures with what each is compared with on a past day", () => {
    const past = answer("house/villa_past_day.json");
    const part = body(past, { detailsOpen: true }, "sv", handlers).querySelector("[data-part='baselines']")!;
    expect(part.querySelector(".spotnav-house-compare-head")?.textContent).toMatch(/^Batteriappens mått, tors 12 nov/);
    expect(part.querySelector("[data-row='battery']")?.textContent).toContain("Batteriet mot inget batteri");
    expect(part.querySelector("[data-row='battery']")?.textContent).toContain("11 kr");
    expect(part.querySelector("[data-row='sun']")?.textContent).toContain("8,2 kr");
    expect(part.querySelector("[data-row='both']")?.textContent).toMatch(/19,50 kr|20 kr/);
    expect(part.textContent).toContain("samma dygn utan batteri");
    expect(part.querySelector("[data-line='baselines-coverage']")).toBeNull();
  });

  it("says how much of a partial day the figures cover and leaves out what is not known", () => {
    const past = answer("house/villa_past_day.json");
    const review = { ...past.day_review!, covered_quarters: 60, sun_quarters: 40, against_nothing: null, sun_gave: null };
    const part = body({ ...past, day_review: review }, { detailsOpen: true }, "sv", handlers).querySelector("[data-part='baselines']")!;
    expect(part.querySelector("[data-line='baselines-coverage']")?.textContent).toBe("60 av dygnets 96 kvartar är mätta.");
    expect(part.querySelector("[data-row='sun']")).toBeNull();
    expect(part.querySelector("[data-row='both']")).toBeNull();
    const sunKnown = { ...past.day_review!, covered_quarters: 96, sun_quarters: 40 };
    const partial = body({ ...past, day_review: sunKnown }, { detailsOpen: true }, "sv", handlers).querySelector("[data-line='baselines-coverage']");
    expect(partial?.textContent).toBe("Solens produktion är känd för 40 av 96.");
  });

  it("shows nothing for a day without them, from an older backend and on a day without a review", () => {
    const past = answer("house/villa_past_day.json");
    expect(body({ ...past, day_review: null }, {}, "sv", handlers).querySelector("[data-part='baselines']")).toBeNull();
    const old = { ...past.day_review!, battery_saving: null, sun_gave: null, against_nothing: null };
    expect(body({ ...past, day_review: old }, {}, "sv", handlers).querySelector("[data-part='baselines']")).toBeNull();
  });

  it("sums the reviewed days on today", () => {
    // Not on the front: under the details.
    expect(body(answer(), {}, "sv").querySelector("[data-part='baselines']")).toBeNull();
    const part = body(answer(), { detailsOpen: true }, "sv").querySelector("[data-part='baselines']")!;
    expect(part.querySelector(".spotnav-house-compare-head")?.textContent).toBe("Batteriappens mått, senaste 7 dygnen");
    expect(part.querySelector("[data-row='battery']")?.textContent).toContain("80 kr");
    expect(part.querySelector("[data-row='sun']")?.textContent).toContain("52 kr");
    expect(part.querySelector("[data-row='both']")).toBeNull();
  });

  it("is worded in every language", () => {
    for (const language of ["en", "sv", "da", "de", "es", "fi", "fr", "nb", "nl"] as const) {
      for (const key of ["headingDay", "headingWeek.one", "headingWeek.other", "battery", "batteryNote", "sun", "sunNote", "both", "bothNote", "partial", "sunPartial"]) {
        expect(translate(language, `house.baselines.${key}` as never, { day: "d", days: "2", covered: "1", total: "2", sun: "1" })).not.toBe(`house.baselines.${key}`);
      }
    }
  });

  it("reads a backend before the figures as none", () => {
    const raw = JSON.parse(JSON.stringify(read("house/villa_past_day.json")));
    for (const key of ["battery_saving", "sun_gave", "against_nothing", "covered_quarters", "day_quarters", "sun_quarters"]) {
      delete raw.day_review[key];
    }
    delete raw.battery.review.battery_saving;
    const result = decodeHouse(raw);
    expect(result.ok).toBe(true);
  });
});

describe("the modes on a past day", () => {
  it("show that day's own review under its date, the day's mode marked", () => {
    const past = withoutPerfect(answer("house/villa_past_day.json"));
    const card = body(past, {}, "sv", { onDetails: vi.fn(), onSelect: vi.fn(), } as never);
    const part = card.querySelector("[data-part='modes']")!;
    expect(part.querySelector(".spotnav-house-compare-head")?.textContent).toMatch(/^Jämfört med om batteriet bara lagrat egen sol, tors 12 nov/);
    const rows = Array.from(part.querySelectorAll<HTMLElement>(".spotnav-house-compare-row"));
    expect(rows.find((row) => row.dataset["row"] === "balanced")?.textContent).toContain("+7 kr");
    expect(rows.find((row) => row.dataset["row"] === "own")?.textContent).toContain("+6 kr");
    expect(rows.find((row) => row.dataset["inForce"])?.dataset["row"]).toBe(past.day_review!.mode);
    expect(part.textContent).not.toContain("dygn)");
  });

  it("say no full day on a past day without a review", () => {
    const past = answer("house/villa_past_day.json");
    const card = body({ ...past, day_review: null }, {}, "sv", { onDetails: vi.fn(), onSelect: vi.fn(), } as never);
    const part = card.querySelector("[data-part='modes']")!;
    expect(part.querySelector(".spotnav-house-compare-head")?.textContent).toBe("Jämfört med om batteriet bara lagrat egen sol: inget fullt dygn än");
    expect(part.querySelector("[data-row='balanced']")?.textContent).toContain("inget fullt dygn");
    expect(translate("fi", "house.compare.headingDay", { day: "d" })).not.toBe("house.compare.headingDay");
  });
});

describe("a past day's answer", () => {
  it("has no day steps on the front: the day choice is in the history", () => {
    const card = body(answer(), {}, "sv", { onDetails: vi.fn(), onSelect: vi.fn(), onHistory: vi.fn() } as never);
    expect(card.querySelector("button[data-step]")).toBeNull();
    expect(card.querySelector("button[data-history='open']")?.getAttribute("aria-label")).toBe("Historik");
  });

  it("still shows a past day's 24 hours, its money and flow under its date", () => {
    const past = answer("house/villa_past_day.json");
    expect(past.day).toEqual({ date: "2026-11-12", today: false, first: "2026-10-31", has_data: true });
    expect(past.hours).toHaveLength(24);
    const card = body(past, {}, "sv", { onDetails: vi.fn(), onSelect: vi.fn() } as never);
    expect(card.querySelector("[data-line='today']")?.textContent).toMatch(/^tors 12 nov\.?: batteriet tjänade/);
    expect(card.querySelector("[data-part='energy-flow'] h3")?.textContent).toMatch(/^Energi tors 12 nov/);
    expect(card.textContent).not.toContain("Hela timmar fram till");
    expect(card.querySelector(".spotnav-house-flow-node[data-role='battery'][data-side='use']")?.textContent).toContain("slut 32 %");
    const endSoc = [...past.hours].reverse().find((hour) => hour.soc_pct !== null)!.soc_pct!;
    expect(card.querySelector(".spotnav-house-sub")?.textContent).toBe(`Batteri ${Math.round(endSoc)} % vid dagens slut · läge Balanserad`);
    expect(translate("en", "house.sub.socDayEnd", { pct: "41 %" })).toBe("Battery 41 % at the end of the day");
  });

  it("says so on a day without data", () => {
    const past = answer("house/villa_past_day.json");
    const card = body({ ...past, day: { ...past.day!, date: "2026-10-31", has_data: false } }, {}, "sv", { onDetails: vi.fn(), onSelect: vi.fn() } as never);
    expect(card.querySelector("[data-line='no-data']")?.textContent).toBe("Inget mättes den här dagen.");
    expect(card.querySelector(".spotnav-house-surface")).toBeNull();
  });

  it("is worded in every language", () => {
    for (const language of LANGUAGES) {
      for (const key of ["house.dayMoney", "house.day.noData", "house.flow.headingDay", "house.compare.heading.other", "house.compare.headingNone", "house.compare.inForce", "house.compare.none", "house.compare.spotnav", "house.compare.own", "house.todayHint", "house.todayCost", "house.compare.days.other"] as const) {
        expect(translate(language, key, { day: "d", days: "2", amount: "", mode: "" })).not.toBe(key);
      }
    }
  });
});

describe("the readout", () => {
  it("stands right under the chart, above the legend, with its lines reserved, and follows a tap", () => {
    const onSelect = vi.fn();
    const card = body(answer(), {}, "en", { onDetails: vi.fn(), onSelect });
    const surface = card.querySelector<HTMLElement>(".spotnav-house-surface")!;
    const readout = card.querySelector(".spotnav-readout")!;
    expect(surface.nextElementSibling).toBe(readout);
    expect(readout.nextElementSibling?.className).toContain("legend");
    expect(readout.classList.contains("spotnav-house-readout")).toBe(true);
    expect(readout.getAttribute("aria-live")).toBe("polite");
    document.body.append(card);
    surface.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: 0 }));
    expect(onSelect).toHaveBeenCalled();
    card.remove();
  });
});

describe("today's energy flow", () => {
  it("decodes the nodes and links, takes a role it does not know and drops a link to a missing node", () => {
    const raw = read("house/villa_battery_sun.json");
    const flow = raw["energy_flow"] as Record<string, unknown>;
    const nodes = [...(flow["nodes"] as object[]), { id: "source.car", side: "source", role: "car", kwh: 1.2, soc_pct: null }, { id: "use.heat", side: "use", role: "heat_pump", kwh: 0.4, soc_pct: null }];
    const links = [...(flow["links"] as object[]), { from: "source.car", to: "use.nowhere", kwh: 1 }];
    const decoded = decodeHouse({ ...raw, energy_flow: { ...flow, nodes, links } });
    expect(decoded.ok).toBe(true);
    const value = decoded.ok ? decoded.value.energy_flow! : null;
    expect(value?.nodes.map((node) => node.role)).toContain("heat_pump");
    expect(value?.links.some((link) => link.to === "use.nowhere")).toBe(false);
    expect(decodeHouse({ ...raw, energy_flow: { ...flow, nodes: [{ id: "x", side: "middle", role: "grid", kwh: 1, soc_pct: null }] } })).toEqual({ ok: false, failure: "malformed" });
    expect(decodeHouse({ ...raw, energy_flow: { ...flow, links: [{ from: "source.grid", to: "use.house", kwh: -1 }] } })).toEqual({ ok: false, failure: "malformed" });
    const { energy_flow: _flow, ...older } = raw;
    expect(decodeHouse(older).ok && (decodeHouse(older) as { value: HouseAnswer }).value.energy_flow).toBeNull();
  });

  it("draws the sources left and the uses right, a band per flow, and words each block with its kWh and share", () => {
    const card = body(answer(), {}, "sv");
    const section = card.querySelector("[data-part='energy-flow']")!;
    expect(section).not.toBeNull();
    const svgRoot = section.querySelector("svg")!;
    expect(Number(svgRoot.getAttribute("width"))).toBe(340);
    const nodes = Array.from(svgRoot.querySelectorAll(".spotnav-house-flow-node")).map((node) => `${node.getAttribute("data-side")}:${node.getAttribute("data-role")}`);
    expect(nodes).toEqual(["source:solar", "source:battery", "source:grid", "use:battery", "use:house", "use:heat", "use:car", "use:grid"]);
    expect(svgRoot.querySelectorAll(".spotnav-house-flow-band")).toHaveLength(answer().energy_flow!.links.length);
    const grid = svgRoot.querySelector(".spotnav-house-flow-node[data-role='grid'][data-side='source']")!;
    expect(Array.from(grid.querySelectorAll("text")).map((text) => text.textContent)).toEqual(["Nät", "76,9 kWh", "83 %"]);
    expect(grid.querySelector(".spotnav-house-flow-pill")).not.toBeNull();
    const sun = svgRoot.querySelector(".spotnav-house-flow-node[data-role='solar']")!;
    // A short block puts its share in the name's pill and the energy alone under it.
    expect(Array.from(sun.querySelectorAll("text")).map((text) => text.textContent)).toEqual(["Sol · 8 %", "7,5 kWh"]);
    const battery = svgRoot.querySelector(".spotnav-house-flow-node[data-role='battery'][data-side='use']")!;
    expect(battery.textContent).toContain("nu 48 %");
    expect(svgRoot.getAttribute("aria-label")).toMatch(/^Sol 7,5 kWh \(8 %\), Batteri 8,6 kWh/);
    expect(section.textContent).toContain("Fram till 17:00.");
    // A flow allocated by the hour says so in the details; by the quarter hour, that.
    expect(section.textContent).not.toContain("varje timme går solen");
    const notes = body(answer(), { detailsOpen: true }, "sv").querySelector("[data-part='flow-notes']")!;
    expect(notes.querySelector("h3")?.textContent).toBe("Så räknas flödet");
    expect(notes.textContent).toContain("varje timme går solen");
    const heat = svgRoot.querySelector(".spotnav-house-flow-node[data-role='heat']")!;
    expect(heat.querySelector("text")?.textContent).toBe("Värme · 14 %");
    // One hour lacked the heat pump's figure: said so.
    expect(notes.textContent).toContain("Värmepumpens mätare kunde inte läsas varje timme");
    // The details do not repeat it as bars.
    expect(body(answer(), { detailsOpen: true }, "sv").querySelector(".spotnav-house-flow-key")).toBeNull();
  });

  it("grows with the number of blocks so the larger uses get the tall form", () => {
    expect(flowHeight(328, 3)).toBe(250);
    expect(flowHeight(328, 4)).toBe(320);
    expect(flowHeight(328, 9)).toBe(440);
    const card = body(answer(), {}, "sv");
    for (const role of ["battery", "car"]) {
      const node = card.querySelector(`.spotnav-house-flow-node[data-side='use'][data-role='${role}']`)!;
      expect(node.querySelector(".spotnav-house-flow-value")).not.toBeNull();
    }
  });

  it("keeps every block tall enough for its words", () => {
    const heights = blockHeights([7.5, 8.6, 76.9], 300);
    expect(Math.min(...heights)).toBeGreaterThanOrEqual(MIN_NODE);
    expect(heights.reduce((a, b) => a + b, 0) + 12).toBeCloseTo(300, 6);
    expect(blockHeights([0, 0], 100)).toEqual([MIN_NODE, MIN_NODE]);
  });

  it("leaves out a use with no energy, and draws nothing without a source", () => {
    const value = answer();
    const flow = value.energy_flow!;
    const noCar = { ...flow, nodes: flow.nodes.map((node) => (node.role === "car" ? { ...node, kwh: 0 } : node)), links: flow.links.filter((link) => link.to !== "use.car") };
    const roles = Array.from(body({ ...value, energy_flow: noCar }).querySelectorAll(".spotnav-house-flow-node")).map((node) => node.getAttribute("data-role"));
    expect(roles).not.toContain("car");
    const empty = { ...flow, nodes: flow.nodes.filter((node) => node.side === "use") };
    // Today: a short line instead of an empty diagram; a past day: nothing.
    const today = body({ ...value, energy_flow: empty }, {}, "sv").querySelector("[data-part='energy-flow']")!;
    expect(today.querySelector("svg")).toBeNull();
    expect(today.textContent).toContain("Dagens energiflöde visas när de första fem minuterna är inne.");
    const past = { ...value, day: { ...value.day!, today: false }, energy_flow: empty };
    expect(body(past).querySelector("[data-part='energy-flow']")).toBeNull();
  });

  it("keeps a month's or a year's figures narrow: no decimal from 100 kWh, a smaller figure from 1 000 kWh", () => {
    const labels = flowLabels("sv", 1000);
    expect(labels.kwh(99.94)).toBe("99,9 kWh");
    expect(labels.kwh(183.4)).toBe("183 kWh");
    expect(labels.kwh(2037)).toBe("2\u00a0037 kWh");
    const value = answer();
    const flow = value.energy_flow!;
    const big = { ...flow, nodes: flow.nodes.map((node) => ({ ...node, kwh: node.kwh * 100 })), links: flow.links.map((link) => ({ ...link, kwh: link.kwh * 100 })) };
    const svgRoot = body({ ...value, energy_flow: big }, {}, "sv").querySelector("[data-part='energy-flow'] svg")!;
    expect(svgRoot.querySelector(".spotnav-house-flow-node[data-role='grid'][data-side='source'] .spotnav-house-flow-value-long")?.textContent).toBe("7\u00a0689 kWh");
  });

  it("says it is on its way in today's first minutes, before the first 5-minute row", () => {
    const value = answer();
    const flow = value.energy_flow!;
    const first = { ...flow, hours: 0, car_hours: 0, heat_hours: 0, step_min: 15, through: "2026-11-12T23:00:00+00:00", nodes: flow.nodes.map((node) => ({ ...node, kwh: 0 })), links: [] };
    const section = body({ ...value, energy_flow: first }, {}, "en").querySelector("[data-part='energy-flow']")!;
    expect(section.querySelector("svg")).toBeNull();
    expect(Array.from(section.querySelectorAll("p")).map((p) => p.textContent)).toEqual(["Today's energy flow appears once the first five minutes are in."]);
  });

  it("follows the day by the quarter hour: says how far it reaches and the quarter rule", () => {
    const value = answer();
    const flow = { ...value.energy_flow!, step_min: 15, through: "2026-11-13T16:15:00+00:00" };
    const card = body({ ...value, energy_flow: flow }, { detailsOpen: true }, "sv");
    expect(card.querySelector("[data-part='energy-flow']")!.textContent).toContain("Fram till 17:15.");
    expect(card.querySelector("[data-part='flow-notes']")!.textContent).toContain("varje kvart går solen");
    const raw = JSON.parse(JSON.stringify(read("house/villa_battery_sun.json")));
    delete raw.energy_flow.step_min;
    const older = decodeHouse(raw);
    expect(older.ok && older.value.energy_flow?.step_min).toBe(60);
    raw.energy_flow.step_min = "15";
    expect(decodeHouse(raw).ok).toBe(false);
  });

  it("draws no block without a band and no band without width", () => {
    const value = answer();
    const flow = {
      ...value.energy_flow!,
      nodes: [
        { id: "source.battery", side: "source" as const, role: "battery", kwh: 6, soc_pct: null },
        { id: "source.grid", side: "source" as const, role: "grid", kwh: 0.06, soc_pct: null },
        { id: "use.house", side: "use" as const, role: "house", kwh: 5.99, soc_pct: null },
        { id: "use.heat", side: "use" as const, role: "heat", kwh: 0.04, soc_pct: null },
        { id: "use.grid", side: "use" as const, role: "grid", kwh: 0.03, soc_pct: null },
      ],
      links: [
        { from: "source.battery", to: "use.house", kwh: 5.98 },
        { from: "source.battery", to: "use.grid", kwh: 0.01 },
        { from: "source.battery", to: "use.heat", kwh: 0.01 },
        { from: "source.grid", to: "use.heat", kwh: 0.03 },
        { from: "source.grid", to: "use.house", kwh: 0.01 },
      ],
    };
    const svgRoot = body({ ...value, energy_flow: flow }).querySelector("[data-part='energy-flow'] svg")!;
    const nodes = Array.from(svgRoot.querySelectorAll(".spotnav-house-flow-node")).map((node) => `${node.getAttribute("data-side")}:${node.getAttribute("data-role")}`);
    expect(nodes).toEqual(["source:battery", "source:grid", "use:house"]);
    const bands = Array.from(svgRoot.querySelectorAll(".spotnav-house-flow-band"));
    expect(bands.map((band) => `${band.getAttribute("data-from")}>${band.getAttribute("data-to")}`)).toEqual(["battery>house", "grid>house"]);
    for (const band of bands) {
      const numbers = (band.getAttribute("d") ?? "").match(/-?\d+(\.\d+)?/g)!.map(Number);
      // M x0,a0 … x0,a1 Z: the band's thickness at its source end.
      expect(numbers[numbers.length - 1]! - numbers[1]!).toBeGreaterThanOrEqual(MIN_BAND - 0.1);
    }
    // Every block drawn has a band at it.
    const ends = new Set(bands.flatMap((band) => [`source:${band.getAttribute("data-from")}`, `use:${band.getAttribute("data-to")}`]));
    expect(nodes.every((node) => ends.has(node))).toBe(true);
  });

  it("is worded in every language", () => {
    for (const language of LANGUAGES) {
      for (const key of ["house.flow.house", "house.flow.car", "house.flow.socNow", "house.flow.through", "house.flow.rule", "house.flow.ruleQuarter", "house.flow.waiting", "house.flow.carPart"] as const) {
        expect(translate(language, key, { pct: "1 %", time: "17:00" })).not.toBe(key);
      }
    }
  });
});

describe("the heat pump", () => {
  it("decodes the heat block and each hour's heat, and an answer before heat as none", () => {
    const full = answer();
    expect(full.heat?.status).toBe("found");
    expect(full.heat?.day).toEqual({ date: "2026-11-13", hours: 17, heat_kwh: 10.57, house_kwh: 20.5 });
    expect(full.heat?.comparison?.hours).toBe(168);
    expect(full.hours.filter((hour) => hour.heat_kw !== null)).toHaveLength(12);
    expect(full.energy_flow?.heat_hours).toBe(16);
    const raw = JSON.parse(JSON.stringify(read("house/villa_battery_sun.json")));
    delete raw.heat;
    delete raw.energy_flow.heat_hours;
    for (const hour of raw.hours) {
      delete hour.heat_kw;
      delete hour.heat_forecast_kw;
      delete hour.car_kw;
    }
    const older = decodeHouse(raw);
    expect(older.ok && older.value.heat).toBeNull();
    expect(older.ok && older.value.hours.every((hour) => hour.heat_kw === null && hour.heat_forecast_kw === null && hour.car_kw === null)).toBe(true);
    // A mistyped heat block makes the answer unreadable, never half-shown.
    const broken = JSON.parse(JSON.stringify(read("house/villa_battery_sun.json")));
    broken.heat.day.heat_kwh = "10";
    expect(decodeHouse(broken).ok).toBe(false);
  });

  it("says the day's heat beside the house, and keys heat in the legend", () => {
    const card = body(answer(), {}, "sv");
    expect(card.querySelector("[data-line='heat']")?.textContent).toBe("Värme i dag: 10,6 kWh av husets 20,5 kWh (52 %)");
    const legend = Array.from(card.querySelectorAll(".spotnav-house-legend li")).map((item) => item.textContent);
    expect(legend).toContain("Värme");
    const learning = body(answer("house/house_learning.json"));
    expect(learning.querySelector("[data-line='heat']")).toBeNull();
    expect(learning.querySelector(".spotnav-house-heat-bar")).toBeNull();
  });

  it("names the meter and the forecast split in the sources", () => {
    const open = body(answer(), { detailsOpen: true }, "en");
    const sources = Array.from(open.querySelectorAll(".spotnav-house-facts li")).map((item) => item.textContent);
    expect(sources).toContain("Heat pump meter: Värme Effekt (chosen in Settings)");
    expect(sources).toContain("House forecast as heat plus the rest: ±0.04 kW against ±0.09 kW for the whole (168 h, shown only)");
  });

  it("asks where to choose when more than one meter could be it, and is quiet once found with no hour yet", () => {
    const value = answer();
    value.heat = { ...value.heat!, status: "ambiguous", entity_id: null, name: null, candidates: ["sensor.hp_power", "sensor.pool_power"], day: null, comparison: null };
    const line = body(value, {}, "en").querySelector("[data-line='heat']")?.textContent;
    expect(line).toBe("More than one meter could be the heat pump's (sensor.hp_power, sensor.pool_power): choose it in Settings, Site.");
    value.heat = { ...value.heat, status: "found", entity_id: "sensor.hp_power", candidates: [] };
    expect(body(value, {}, "en").querySelector("[data-line='heat']")).toBeNull();
    value.heat = { ...value.heat, status: "unusable" };
    expect(body(value, {}, "en").querySelector("[data-line='heat']")?.textContent).toContain("neither a power nor an energy sensor");
  });

  it("is worded in every language", () => {
    const value = answer();
    for (const language of LANGUAGES) {
      const text = body(value, { detailsOpen: true }, language).textContent ?? "";
      expect(text, language).toContain(translate(language, "house.flow.heat"));
      expect(text, language).not.toMatch(/house\.heat|house\.sources\.heat|\{\w+\}/);
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
      labels: { kw: String, price: String, priceUnit: "öre", now: "Now", hour: () => "", day: () => "" },
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

  it("asks only for today; the clock opens the history at the running month and steps by period", async () => {
    const hass = new FakeHass();
    const element = mount({ type: "custom:spotnav-house-card", site: "site_villa" }, hass);
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(read("house/villa_battery_sun.json"));
    await vi.advanceTimersByTimeAsync(0);
    const root = element.shadowRoot!;
    root.querySelector<HTMLButtonElement>("button[data-history='open']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages[1]).toEqual({ type: "spotnav/get_house_history", api_version: 1, site_id: "site_villa", period: "month" });
    hass.resolveNext(read("house_history/villa_month.json"));
    await vi.advanceTimersByTimeAsync(0);
    const dialog = root.querySelector<HTMLElement>("[role='dialog']")!;
    expect(dialog.querySelector("[data-part='energy-flow'] svg")).not.toBeNull();
    expect(dialog.querySelectorAll(".spotnav-history-bar")).toHaveLength(30);
    dialog.querySelector<HTMLButtonElement>("button[data-step='back']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages[2]).toMatchObject({ period: "month", date: "2026-10-01" });
    hass.resolveNext(read("house_history/villa_month.json"));
    await vi.advanceTimersByTimeAsync(0);
    dialog.querySelector<HTMLButtonElement>("button[data-period='day']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages[3]).toMatchObject({ type: "spotnav/get_house_history", period: "day" });
    hass.resolveNext(read("house_history/villa_day.json"));
    await vi.advanceTimersByTimeAsync(0);
    expect(dialog.querySelectorAll(".spotnav-history-bar")).toHaveLength(24);
    await vi.advanceTimersByTimeAsync(300_000);
    expect(hass.messages.filter((message) => message.type === "spotnav/get_house").every((message) => !("date" in message))).toBe(true);
    element.remove();
  });

  it("toggles a legend series, remembers it in this browser and finds it again in a new card; without storage it still toggles", async () => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, value: string) => void store.set(key, value) });
    const load = async (): Promise<SpotnavHouseCard> => {
      const hass = new FakeHass();
      const element = mount({ type: "custom:spotnav-house-card", site: "site_villa" }, hass);
      await vi.advanceTimersByTimeAsync(0);
      hass.resolveNext(read("house/villa_battery_sun.json"));
      await vi.advanceTimersByTimeAsync(0);
      return element;
    };
    const first = await load();
    const root = first.shadowRoot!;
    expect(root.querySelector("svg .spotnav-house-car-bar")).toBeNull();
    root.querySelector<HTMLButtonElement>("button[data-legend='car']")!.click();
    expect(root.querySelector("svg .spotnav-house-car-bar")).not.toBeNull();
    expect(root.querySelector("button[data-legend='car']")!.getAttribute("aria-pressed")).toBe("true");
    root.querySelector<HTMLButtonElement>("button[data-legend='sun']")!.click();
    expect(root.querySelector("svg .spotnav-house-sun-area")).toBeNull();
    expect(JSON.parse(store.get("spotnav-card.house.hidden.energy")!)).toEqual(["sun"]);
    // The price view keeps its own choice.
    root.querySelector<HTMLButtonElement>("button[data-view='price']")!.click();
    expect(root.querySelector("button[data-legend='buy']")!.getAttribute("aria-pressed")).toBe("true");
    root.querySelector<HTMLButtonElement>("button[data-legend='buy']")!.click();
    expect(root.querySelector("svg .spotnav-house-price-buy")).toBeNull();
    expect(store.has("spotnav-card.house.hidden.price")).toBe(true);
    first.remove();
    const again = await load();
    expect(again.shadowRoot!.querySelector("svg .spotnav-house-car-bar")).not.toBeNull();
    expect(again.shadowRoot!.querySelector("svg .spotnav-house-sun-area")).toBeNull();
    again.shadowRoot!.querySelector<HTMLButtonElement>("button[data-view='price']")!.click();
    expect(again.shadowRoot!.querySelector("svg .spotnav-house-price-buy")).toBeNull();
    again.remove();
    // No storage at all: the defaults, and a toggle still works for the page's life.
    vi.stubGlobal("localStorage", undefined);
    const bare = await load();
    expect(bare.shadowRoot!.querySelector("svg .spotnav-house-car-bar")).toBeNull();
    bare.shadowRoot!.querySelector<HTMLButtonElement>("button[data-legend='car']")!.click();
    expect(bare.shadowRoot!.querySelector("svg .spotnav-house-car-bar")).not.toBeNull();
    bare.remove();
    vi.unstubAllGlobals();
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
