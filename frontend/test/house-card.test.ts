// The house card (SpotNav 3.0 preview): the decoder on the backend's own fixtures, the body's chart, lines and
// details, and the element asking `spotnav/get_house` for its site.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { HOUSE_CARD_ELEMENT, parseHouseConfig, SpotnavHouseCard } from "../src/house-card";
import { houseChartHeight, renderHouseChart } from "../src/house-chart";
import { blockHeights, flowHeight, MIN_NODE } from "../src/house-flow";
import { HOUSE_STYLES } from "../src/house-styles";
import { decodeHouse, type HouseAnswer } from "../src/house-model";
import { amount, hourFigures, houseBody, reviewLine, savingLine, todayLine, type HouseState, type HouseUi } from "../src/house-view";
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
    expect(savingLine("en", value)).toBe("Last 7 days: SpotNav's plan 51 kr · the battery itself 45 kr, against plain self-consumption");
    expect(savingLine("sv", value)).toBe("Senaste 7 dygnen: SpotNavs plan 51 kr · batteriet självt 45 kr, mot ren egenförbrukning");
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
    expect(card.querySelector("[data-line='saving']")).toBeNull();
    expect(card.querySelector("[data-part='modes']")?.textContent).toContain("51 kr");
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
      ["Batteriets egen optimerare", "–", "45 kr"],
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

describe("the battery app's views, calm", () => {
  it("say today's money in one line, split as the battery's app does, with the mode in force", () => {
    expect(todayLine("sv", answer())).toBe("I dag: sålt 0,04 kr · arbitrage 11 kr · köpt 38 kr · läge Balanserad");
    expect(todayLine("sv", answer("house/house_learning.json"))).toBeNull();
    const card = body(answer(), {}, "sv");
    expect(card.querySelector("[data-line='today']")?.textContent).toContain("arbitrage");
    expect(card.querySelector(".spotnav-house-sub")?.textContent).toBe("Batteri 48 %");
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
    const surface = price.querySelector<HTMLElement>(".spotnav-house-surface")!;
    document.body.append(price);
    surface.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(price.querySelector(".spotnav-readout")?.textContent).toMatch(/^17:00–18:00 · buy 3\d\d öre\/kWh · sell \d+ öre\/kWh · battery/);
  });

  it("offer no price view where nothing is priced", () => {
    expect(body(answer("house/house_learning.json")).querySelector("button[data-view]")).toBeNull();
  });

  it("show today's energy from sources to uses and the month's days in the details", () => {
    // Without the energy flow (an older backend) the details keep the two bars.
    const card = body({ ...answer(), energy_flow: null }, { detailsOpen: true }, "sv");
    const keys = Array.from(card.querySelectorAll(".spotnav-house-flow-key")).map((item) => item.textContent);
    expect(keys[0]).toMatch(/^Sol 4,5 kWh \(\d+ %\) · Batteri 9,8 kWh \(\d+ %\) · Nät 34,3 kWh \(\d+ %\)$/);
    expect(keys[1]).toMatch(/^Batteri 27,1 kWh/);
    const days = card.querySelectorAll(".spotnav-house-calendar-day[data-date]");
    expect(days).toHaveLength(30);
    expect(card.querySelectorAll(".spotnav-house-calendar-day[data-result='plan']")).toHaveLength(9);
    expect(card.querySelectorAll(".spotnav-house-calendar-day[data-result='own']")).toHaveLength(3);
    expect(card.querySelector(".spotnav-house-calendar-day[data-today]")?.getAttribute("data-date")).toBe("2026-11-13");
    expect(card.querySelector(".spotnav-house-calendar-day[data-date='2026-11-04']")?.getAttribute("aria-label")).toMatch(/^ons 4 nov\.?: SpotNavs plan/);
    expect(card.querySelector(".spotnav-house-calendar-day[data-date='2026-11-04']")?.getAttribute("aria-label")).toMatch(/ · arbitrage 8,00 kr$/);
    expect(card.textContent).toContain("Batteriets arbitrage, 12 dygn:");
    expect(card.textContent).toContain("Lagrat nu: 32,3 kWh à 121 öre/kWh i snitt");
  });
});

describe("the modes side by side in the body", () => {
  it("compare every mode and the battery itself against plain self-consumption, the mode in force marked", () => {
    const value = answer();
    const card = body(value, {}, "sv");
    const part = card.querySelector("[data-part='modes']")!;
    expect(part.querySelector(".spotnav-house-compare-head")?.textContent).toBe("Mot ren egenförbrukning, senaste 7 dygnen");
    const rows = Array.from(part.querySelectorAll<HTMLElement>(".spotnav-house-compare-row"));
    expect(rows.map((row) => row.dataset["row"])).toEqual(["self_consumption", "balanced", "max_profit", "own"]);
    expect(rows.filter((row) => row.dataset["inForce"] === "true").map((row) => row.dataset["row"])).toEqual([value.battery!.mode]);
    expect(rows.find((row) => row.dataset["inForce"])?.textContent).toContain("gäller");
    expect(rows[3]!.textContent).toContain("45 kr");
    expect(rows[0]!.textContent).toContain("referens");
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

describe("the day choice", () => {
  it("steps back a day from today, never forward past it", () => {
    const onDay = vi.fn();
    const card = body(answer(), {}, "sv", { onDetails: vi.fn(), onSelect: vi.fn(), onDay } as never);
    const [back, forward] = Array.from(card.querySelectorAll<HTMLButtonElement>("button[data-step]"));
    expect(card.querySelector(".spotnav-house-day-label")?.textContent).toBe("I dag");
    expect(forward!.disabled).toBe(true);
    back!.click();
    expect(onDay).toHaveBeenCalledWith("2026-11-12");
  });

  it("shows a past day's 24 hours, its money and flow under its date, forward back toward today", () => {
    const onDay = vi.fn();
    const past = answer("house/villa_past_day.json");
    expect(past.day).toEqual({ date: "2026-11-12", today: false, first: "2026-10-31", has_data: true });
    expect(past.hours).toHaveLength(24);
    const card = body(past, {}, "sv", { onDetails: vi.fn(), onSelect: vi.fn(), onDay } as never);
    expect(card.querySelector(".spotnav-house-day-label")?.textContent).toMatch(/^tors 12 nov/);
    expect(card.querySelector("[data-line='today']")?.textContent).toMatch(/^tors 12 nov\.?: sålt/);
    expect(card.querySelector("[data-part='energy-flow'] h3")?.textContent).toMatch(/^Energi tors 12 nov/);
    expect(card.textContent).not.toContain("Hela timmar fram till");
    expect(card.querySelector(".spotnav-house-flow-node[data-role='battery'][data-side='use']")?.textContent).toContain("slut 32 %");
    const endSoc = [...past.hours].reverse().find((hour) => hour.soc_pct !== null)!.soc_pct!;
    expect(card.querySelector(".spotnav-house-sub")?.textContent).toBe(`Batteri ${Math.round(endSoc)} % vid dagens slut`);
    expect(translate("en", "house.sub.socDayEnd", { pct: "41 %" })).toBe("Battery 41 % at the end of the day");
    card.querySelectorAll<HTMLButtonElement>("button[data-step]")[1]!.click();
    expect(onDay).toHaveBeenCalledWith("2026-11-13");
  });

  it("says so on a day without data, and stops at the first day", () => {
    const past = answer("house/villa_past_day.json");
    const card = body({ ...past, day: { ...past.day!, date: "2026-10-31", has_data: false } }, {}, "sv", { onDetails: vi.fn(), onSelect: vi.fn(), onDay: vi.fn() } as never);
    expect(card.querySelector("[data-line='no-data']")?.textContent).toBe("Inget mättes den här dagen.");
    expect(card.querySelector(".spotnav-house-surface")).toBeNull();
    expect(card.querySelectorAll<HTMLButtonElement>("button[data-step]")[0]!.disabled).toBe(true);
  });

  it("has step buttons of 28 px, above the 24 px minimum target", () => {
    expect(HOUSE_STYLES).toMatch(/\.spotnav-house-day-step \{[^}]*height: 28px;[^}]*min-height: 28px;/);
  });

  it("is worded in every language", () => {
    for (const language of LANGUAGES) {
      for (const key of ["house.dayMoney", "house.day.choose", "house.day.today", "house.day.back", "house.day.forward", "house.day.noData", "house.flow.headingDay", "house.compare.heading.other", "house.compare.headingNone", "house.compare.inForce", "house.compare.none", "house.compare.reference", "house.compare.days.other"] as const) {
        expect(translate(language, key, { day: "d", days: "2", sold: "", arbitrage: "", bought: "", mode: "" })).not.toBe(key);
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
    expect(nodes).toEqual(["source:solar", "source:battery", "source:grid", "use:battery", "use:house", "use:car", "use:grid"]);
    expect(svgRoot.querySelectorAll(".spotnav-house-flow-band")).toHaveLength(answer().energy_flow!.links.length);
    const grid = svgRoot.querySelector(".spotnav-house-flow-node[data-role='grid'][data-side='source']")!;
    expect(Array.from(grid.querySelectorAll("text")).map((text) => text.textContent)).toEqual(["Nät", "76,9 kWh", "83 %"]);
    expect(grid.querySelector(".spotnav-house-flow-pill")).not.toBeNull();
    const sun = svgRoot.querySelector(".spotnav-house-flow-node[data-role='solar']")!;
    expect(Array.from(sun.querySelectorAll("text")).map((text) => text.textContent)).toEqual(["Sol", "7,5 kWh · 8 %"]);
    const battery = svgRoot.querySelector(".spotnav-house-flow-node[data-role='battery'][data-side='use']")!;
    expect(battery.textContent).toContain("nu 48 %");
    expect(svgRoot.getAttribute("aria-label")).toMatch(/^Sol 7,5 kWh \(8 %\), Batteri 8,6 kWh/);
    expect(section.textContent).toContain("Hela timmar fram till 17:00.");
    // The details do not repeat it as bars.
    expect(body(answer(), { detailsOpen: true }, "sv").querySelector(".spotnav-house-flow-key")).toBeNull();
  });

  it("grows with the number of blocks so the house and the car get the tall form", () => {
    expect(flowHeight(328, 3)).toBe(250);
    expect(flowHeight(328, 4)).toBe(320);
    expect(flowHeight(328, 9)).toBe(440);
    const card = body(answer(), {}, "sv");
    for (const role of ["house", "car"]) {
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
    expect(body({ ...value, energy_flow: empty }).querySelector("[data-part='energy-flow']")).toBeNull();
  });

  it("is worded in every language", () => {
    for (const language of LANGUAGES) {
      for (const key of ["house.flow.house", "house.flow.car", "house.flow.socNow", "house.flow.through", "house.flow.rule", "house.flow.carPart"] as const) {
        expect(translate(language, key, { pct: "1 %", time: "17:00" })).not.toBe(key);
      }
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

  it("asks for a past day by date when stepped back, and for today without one when stepped forward onto it", async () => {
    const hass = new FakeHass();
    const element = mount({ type: "custom:spotnav-house-card", site: "site_villa" }, hass);
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(read("house/villa_battery_sun.json"));
    await vi.advanceTimersByTimeAsync(0);
    element.shadowRoot!.querySelector<HTMLButtonElement>("button[data-step='back']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages[1]).toEqual({ type: "spotnav/get_house", api_version: 1, site_id: "site_villa", date: "2026-11-12" });
    hass.resolveNext(read("house/villa_past_day.json"));
    await vi.advanceTimersByTimeAsync(0);
    expect(element.shadowRoot!.querySelector(".spotnav-house-day-label")?.textContent).toMatch(/12/);
    element.shadowRoot!.querySelector<HTMLButtonElement>("button[data-step='forward']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages[2]).toMatchObject({ date: "2026-11-13" });
    hass.resolveNext(read("house/villa_battery_sun.json"));
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(300_000);
    expect(hass.messages[3]).not.toHaveProperty("date");
    element.remove();
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
