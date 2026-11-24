// The house and heat cards' history (`house-history.ts`, `heat-history.ts`, `period-history.ts`): the strict decoders on
// the backend's own fixtures, the period steps and pickers, the Detailed switch remembered per browser, and what each
// period shows, including a day of 25 hours and the heat card's hot-water runs.

import { HOUSE_STYLES } from "../src/house-styles";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { decodeHeatHistory, heatHistoryBody, type HeatHistory } from "../src/heat-history";
import { decodeHouseHistory, houseHistoryBody, type HouseHistory } from "../src/house-history";
import { LANGUAGES, registerTranslation, translate } from "../src/i18n";
import { TRANSLATIONS } from "../src/i18n/all";
import { HEAT_CARD_ELEMENT, SpotnavHeatCard } from "../src/heat-card";
import { defineCard } from "../src/index";
import {
  detailedKey,
  periodLabel,
  periodStart,
  pickerMonths,
  readDetailed,
  shiftPeriod,
  writeDetailed,
  type PeriodUi,
} from "../src/period-history";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function read(path: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, path), "utf8")) as Record<string, unknown>;
}

function house(path: string): HouseHistory {
  const decoded = decodeHouseHistory(read(path));
  if (!decoded.ok) {
    throw new Error(`${path} must decode`);
  }
  return decoded.value;
}

function heat(path: string): HeatHistory {
  const decoded = decodeHeatHistory(read(path));
  if (!decoded.ok) {
    throw new Error(`${path} must decode`);
  }
  return decoded.value;
}

function ui(over: Partial<PeriodUi> = {}): PeriodUi {
  return { period: "month", date: null, detailed: false, pending: false, selected: null, ...over };
}

const handlers = () => ({ onPeriod: vi.fn(), onDetailed: vi.fn() });

beforeAll(() => {
  registerTranslation("sv", TRANSLATIONS.sv);
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("the periods", () => {
  it("step by day, month and year, across a new year", () => {
    expect(shiftPeriod("day", "2026-01-01", -1)).toBe("2025-12-31");
    expect(shiftPeriod("month", "2026-01-15", -1)).toBe("2025-12-01");
    expect(shiftPeriod("month", "2025-12-31", 1)).toBe("2026-01-01");
    expect(shiftPeriod("year", "2026-06-01", -1)).toBe("2025-01-01");
    expect(periodStart("month", "2026-11-16")).toBe("2026-11-01");
    expect(periodStart("year", "2026-11-16")).toBe("2026-01-01");
    expect(periodLabel("sv", "month", "2026-11-01")).toBe("november 2026");
    expect(periodLabel("sv", "year", "2026-01-01")).toBe("2026");
  });

  it("offer months back from today, newest first, and the one shown", () => {
    const months = pickerMonths("2026-11-01", "2023-05-01");
    expect(months[0]).toBe("2026-11-01");
    expect(months).toContain("2023-05-01");
    expect(months.every((month) => month <= "2026-11-01")).toBe(true);
  });

  it("remember Detailed in this browser, and keep working without storage", () => {
    const store = new Map<string, string>();
    const local = () => ({ getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, value: string) => void store.set(key, value) });
    expect(readDetailed("house", local)).toBe(false);
    writeDetailed("house", true, local);
    expect(store.get(detailedKey("house"))).toBe("1");
    expect(readDetailed("house", local)).toBe(true);
    expect(readDetailed("heat", local)).toBe(false);
    const broken = () => {
      throw new Error("blocked");
    };
    expect(readDetailed("house", broken)).toBe(false);
    expect(() => writeDetailed("house", true, broken)).not.toThrow();
  });
});

describe("the house history", () => {
  it("decodes every fixture and refuses a newer version or a mistyped field", () => {
    for (const name of ["villa_month.json", "villa_day.json", "house_alone_year.json", "solar_alone_day.json", "solar_alone_month.json"]) {
      expect(decodeHouseHistory(read(`house_history/${name}`)).ok).toBe(true);
    }
    expect(decodeHouseHistory({ ...read("house_history/villa_month.json"), api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    expect(decodeHouseHistory({ ...read("house_history/villa_month.json"), period: "week" })).toEqual({ ok: false, failure: "malformed" });
    // An added key is fine.
    expect(decodeHouseHistory({ ...read("house_history/villa_month.json"), later: 1 }).ok).toBe(true);
  });

  it("names one day count for the savings, and a row's own only where it has fewer days", () => {
    const answer = house("house_history/villa_month.json");
    answer.review!.battery_days = 1;
    const body = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    const money = body.querySelector("[data-part='money']")!;
    expect(money.querySelector("[data-row='battery']")?.textContent).toMatch(/52 kr på 1 dygn$/);
    expect(money.querySelector("[data-row='sun']")?.textContent).not.toContain("dygn");
    expect(money.querySelector("[data-line='reviewed']")?.textContent).toBe("Sparat är mätt för 13 av periodens 15 dygn.");
  });

  it("shows the sun's value in the money of a site without a battery, and says where days are estimated", () => {
    const answer = house("house_history/solar_alone_month.json");
    const body = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    const money = body.querySelector("[data-part='money']")!;
    expect(money.querySelector("[data-row='sun']")?.textContent).toMatch(/^Solen sparade.*135 kr$/);
    expect(money.querySelector("[data-line='sun-estimated']")?.textContent).toBe(translate("sv", "house.sun.estimated"));
    // No battery review is missing on a site without a battery.
    expect(money.querySelector("[data-line='no-review']")).toBeNull();
    // Fewer days valued than the period's: the row names them.
    answer.sun!.days = 3;
    const fewer = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(fewer.querySelector("[data-part='money'] [data-row='sun']")?.textContent).toMatch(/på 3 dygn$/);
  });

  it("shows a month: the card's picker, a bar a day, the money and every section; no flow, no forward step past today", () => {
    const answer = house("house_history/villa_month.json");
    const on = handlers();
    const body = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui(), handlers: on, width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelectorAll(".spotnav-history-bar")).toHaveLength(30);
    expect(body.querySelector("button[data-step='forward']")!.hasAttribute("disabled")).toBe(true);
    expect(body.querySelector<HTMLSelectElement>("select[data-pick='month']")!.value).toBe("2026-11-01");
    // The energy flow is the card's own tab; the picker is the card's (its label over the list, Day | Month | Year).
    expect(body.querySelector("[data-part='energy-flow']")).toBeNull();
    expect(body.querySelector(".spotnav-house-picker [data-pick-label]")?.textContent).toBe("nov 2026");
    expect(body.querySelector("button[data-kind='month']")?.getAttribute("aria-pressed")).toBe("true");
    const money = body.querySelector("[data-part='money']")!;
    expect(money.querySelector("[data-row='cost']")?.textContent).toMatch(/^Elkostnad/);
    expect(money.querySelector("[data-row='battery']")?.textContent).toMatch(/^Batteriet sparade.*52 kr/);
    expect(money.querySelector("[data-line='reviewed']")?.textContent).toBe("Sparat är mätt för 13 av periodens 15 dygn.");
    // Nothing is behind a switch: the modes, the forecasts and the days are always there.
    expect(body.querySelector("[data-detailed]")).toBeNull();
    // Each figure once: the bars, the perfect plan with the battery's ways, the money, the forecasts, the days and
    // last how it is counted. The modes against self-consumption stand in only before the first perfect plan.
    expect(Array.from(body.querySelectorAll<HTMLElement>("section[data-part]")).map((item) => item.dataset["part"])).toEqual(["perfect", "money", "spotnav-saving", "forecast", "days", "how"]);
    body.querySelector<HTMLButtonElement>("button[data-step='back']")!.click();
    expect(on.onPeriod).toHaveBeenCalledWith("month", "2026-10-01");
    body.querySelector<HTMLButtonElement>("button[data-kind='year']")!.click();
    expect(on.onPeriod).toHaveBeenCalledWith("year", null);
  });

  it("shows the modes, the forecasts' errors and each day", () => {
    const answer = house("house_history/villa_day.json");
    let body = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui({ period: "day", detailed: true }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelectorAll(".spotnav-history-bar")).toHaveLength(24);
    expect(body.querySelector<HTMLInputElement>("input[data-pick='day']")!.value).toBe("2026-11-15");
    expect(body.querySelector<HTMLInputElement>("input[data-pick='day']")!.max).toBe("2026-11-16");
    // With a perfect plan the ways stand in its table; before the first one, the modes against self-consumption.
    expect(body.querySelector("[data-part='modes']")).toBeNull();
    const before = { ...answer, review: { ...answer.review!, perfect: null } };
    const body_ = body;
    body = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: before }, ui: ui({ period: "day" }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelector("[data-part='perfect']")).toBeNull();
    expect(body.querySelector("[data-part='modes'] [data-row='max_profit']")?.textContent).toContain("2,00 kr");
    // Self-consumption is the zero the others are measured against, not a row.
    expect(body.querySelector("[data-part='modes'] [data-row='self_consumption']")).toBeNull();
    expect(body.querySelector("[data-part='modes'] [data-row='own']")?.textContent).toContain("Batteriets egen styrning");
    expect(body_.querySelector("[data-part='forecast'] [data-row='house']")?.textContent).toBe("Husets last±0,42 kW (1 d)");
    // A day has no day-by-day list.
    expect(body_.querySelector("[data-part='days']")).toBeNull();
    const month = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: house("house_history/villa_month.json") }, ui: ui({ detailed: true }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    // A table: a row a day, a column a figure.
    expect(month.querySelectorAll("[data-part='days'] tbody tr")).toHaveLength(13);
    // A column a figure, and the battery's own share of its perfect plan (none before the first perfect plan).
    expect(month.querySelectorAll("[data-part='days'] tbody tr:first-child td")).toHaveLength(5);
    expect(month.querySelector("[data-part='days'] tr[data-date='2026-11-03'] td:last-child")?.textContent).toBe("–");
    expect(month.querySelector("[data-part='days'] tr[data-date='2026-11-06'] td:last-child")?.textContent).toBe("78 %");
    // A day under 90 % measured, and one whose perfect plan saved under 5 kr, say why they have no share.
    expect(month.querySelector("[data-part='days'] tr[data-date='2026-11-07'] td:last-child")?.textContent).toBe("ofullständigt dygn");
    expect(month.querySelector("[data-part='days'] tr[data-date='2026-11-09'] td:last-child")?.textContent).toBe("för liten skillnad");
    // A day the battery's own way lost money against no battery says so, not "0 %".
    const villa = house("house_history/villa_month.json");
    const byDay = villa.review!.by_day.map((day) => (day.date === "2026-11-06" ? { ...day, own_share: 0, share_note: "loss" as const } : day));
    const lost = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: { ...villa, review: { ...villa.review!, by_day: byDay } } }, ui: ui({ detailed: true }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(lost.querySelector("[data-part='days'] tr[data-date='2026-11-06'] td:last-child")?.textContent).toBe("förlust");
  });

  it("opens a month with the shares of the perfect plan, summed so the day boundary washes out", () => {
    const answer = house("house_history/villa_month.json");
    const body = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    const part = body.querySelector("[data-part='perfect']")!;
    expect(part.querySelector("h4")?.textContent).toBe("Andel av perfekt");
    expect(part.querySelector("[data-line='perfect-headline']")).toBeNull();
    expect(part.querySelector("[data-line='perfect-explain']")?.textContent).toBe("Perfekt: bästa möjliga i efterhand, får sälja (Max vinst-regler).");
    expect(part.querySelector("[data-line='perfect-midnight']")).toBeNull();
    expect(part.querySelector("[data-line='perfect-partial']")?.textContent).toBe("1 ofullständigt dygn räknas inte med.");
    // It comes before the money, with one table of what each way saved and the battery's work: a block per way.
    const parts = Array.from(body.querySelectorAll<HTMLElement>("[data-part]")).map((item) => item.dataset["part"]);
    expect(parts.indexOf("perfect")).toBeLessThan(parts.indexOf("money"));
    expect(body.querySelector("[data-part='perfect-details']")).toBeNull();
    expect(part.querySelectorAll("table")).toHaveLength(1);
    expect(Array.from(part.querySelectorAll("thead th")).map((cell) => cell.textContent)).toEqual(["", "Sparat", "Cykler", "Växlingar", "Från nätet", "Till nätet"]);
    // The name twice: its own row (a phone) and the first cell of the figures (a wide dialog); CSS shows one.
    const ways = Array.from(part.querySelectorAll<HTMLElement>("table tbody")).map((group) => [group.dataset["way"], ...Array.from(group.querySelectorAll("th, td")).map((cell) => cell.textContent)]);
    expect(ways).toEqual([
      ["own", "Batteriets egen styrning", "Batteriets egen styrning", "72 kr", "3,69", "36", "81,0", "3,6"],
      ["balanced", "SpotNav Balanserad", "SpotNav Balanserad", "69 kr", "3,80", "18", "112,5", "0,0"],
      ["max_profit", "SpotNav Max vinst", "SpotNav Max vinst", "64 kr", "4,91", "27", "135,0", "49,5"],
      ["self_consumption", "SpotNav Egenförbrukning", "SpotNav Egenförbrukning", "52 kr", "2,68", "9", "0,0", "0,0"],
      ["perfect", "Perfekt", "Perfekt", "94 kr", "4,02", "18", "126,0", "54,0"],
    ]);
    // A share stands once, on its bar; the table does not repeat it.
    expect(Array.from(part.querySelectorAll(".spotnav-house-compare-value")).map((cell) => cell.textContent)).toEqual(["77 %", "74 %", "69 %", "55 %"]);
    expect(part.querySelector("table")!.textContent).not.toContain("%");
    // The rules behind the figures: the next day's mean price at midnight, and 5 kr a day for a share.
    expect(part.textContent).toContain("nästa dygns medelpris för köpt el");
    expect(part.textContent).toContain("minst 5 kr per dygn");
    // The battery against no battery stands under Money too, over other days and without what was left at midnight:
    // the two sums are said side by side with why they differ.
    expect(body.querySelector("[data-part='money'] [data-row='battery']")?.textContent).toMatch(/52 kr$/);
    expect(part.querySelector("[data-line='perfect-differs']")?.textContent).toBe(
      "Batteriets egen styrning sparade 72 kr här, medan Batteriet sparade under Pengar visar 52 kr. Båda är mot inget batteri, men här räknas bara hela dygn (9) och det som fanns kvar i batteriet vid midnatt; under Pengar räknas alla mätta dygn (13) och inte det som fanns kvar.",
    );
    // A table's headings wrap between their words, so its columns fit a phone; the day table keeps each date on one
    // line.
    expect(HOUSE_STYLES).toMatch(/\.spotnav-house-table thead th \{\s*white-space: normal;/);
    expect(HOUSE_STYLES).toMatch(/\[data-part="days"\] \.spotnav-house-table td:first-child \{\s*white-space: nowrap;/);
    expect(part.querySelector("[data-line='cell-temperature']")?.textContent).toBe("Battericeller: medel 21,8 °C, högst 27,5 °C, 15 min över 30 °C (åtta dygn).");
  });

  it("says which rules the perfect plan kept, a share clamped and a way too close to compare", () => {
    const answer = house("house_history/villa_month.json");
    const perfect = answer.review!.perfect!;
    const shown = (over: Partial<typeof perfect>): HTMLElement =>
      houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: { ...answer, review: { ...answer.review!, perfect: { ...perfect, ...over } } } }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" }).querySelector<HTMLElement>("[data-part='perfect']")!;
    expect(shown({ rules: "no_sell" }).querySelector("[data-line='perfect-explain']")?.textContent).toBe("Perfekt: bästa möjliga i efterhand, utan försäljning (inget gick ut på nätet).");
    expect(shown({ rules: "mode" }).querySelector("[data-line='perfect-explain']")?.textContent).toBe("Perfekt: bästa möjliga i efterhand, med samma regler.");
    const rows = Array.from(shown({}).querySelectorAll<HTMLElement>(".spotnav-house-compare-row"));
    expect(rows.map((row) => row.dataset["row"])).toEqual(["own", "balanced", "max_profit", "self_consumption"]);
    expect(rows[1]!.dataset["inForce"]).toBe("true");
    expect(rows[0]!.querySelector<HTMLElement>(".spotnav-house-compare-fill")!.style.width).toBe("77%");
    expect(shown({}).querySelector("[data-line='perfect-provisional']")?.textContent).toBe(
      "Det som fanns kvar vid midnatt räknas med dygnets eget pris för en av dygnen, tills nästa dygns priser finns.",
    );
    const clamped = shown({ clamped: ["own"], shares: { ...perfect.shares, own: 100, max_profit: null } });
    expect(clamped.querySelector("[data-line='perfect-clamped']")?.textContent).toBe("Batteriets egen styrning: över 100 %, visas som 100 % (modellens batteri är idealt).");
    expect(clamped.querySelector("[data-row='max_profit'] .spotnav-house-compare-value")?.textContent).toBe("för liten skillnad att jämföra");
    // A way that lost money against no battery says so with its amount, not "0 %", and is not in the clamp note.
    const loss = shown({ clamped: ["own"], shares: { ...perfect.shares, own: 0 }, savings: { ...perfect.savings, own: -4700 } });
    expect(loss.querySelector("[data-row='own'] .spotnav-house-compare-value")?.textContent).toBe("förlust 47 kr");
    expect(loss.querySelector("[data-row='own'] .spotnav-house-compare-fill")).toBeNull();
    expect(loss.querySelector("[data-line='perfect-headline']")).toBeNull();
    expect(loss.querySelector("[data-line='perfect-clamped']")).toBeNull();
    // A way that did not run on these days has no bar and no block; the battery's own figure not held, no note on it.
    const none = shown({ savings: { ...perfect.savings, max_profit: null, own: null }, shares: { ...perfect.shares, max_profit: null, own: null } });
    expect(none.querySelector("tbody[data-way='max_profit']")).toBeNull();
    expect(none.querySelector("[data-line='perfect-differs']")).toBeNull();
  });

  it("stands back for the modes against self-consumption while only incomplete days have a perfect plan", () => {
    const answer = house("house_history/villa_month.json");
    const none = { own: null, self_consumption: null, balanced: null, max_profit: null };
    const partial = { ...answer.review!.perfect!, days: 0, partial: 2, saving: null, shares: none, savings: none, provisional: 0, work: { ...none, perfect: null } };
    const body = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: { ...answer, review: { ...answer.review!, perfect: partial } } }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelector("[data-part='perfect']")).toBeNull();
    const modes = body.querySelector("[data-part='modes']")!;
    expect(modes.querySelector("[data-row='balanced']")?.textContent).toContain("20 kr");
    expect(modes.querySelector("[data-line='perfect-partial']")?.textContent).toBe("Bara 2 ofullständiga dygn: inget att jämföra än.");
    expect(modes.querySelector("table")).toBeNull();
    expect(modes.querySelector("[data-line='cell-temperature']")).not.toBeNull();
  });

  it("says last how it is counted: what SpotNav's saving is compared with and how the flow is allocated", () => {
    const answer = house("house_history/villa_month.json");
    const render = (value: HouseHistory, extra: { carSince?: string | null } = {}): HTMLElement =>
      houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: value }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16", ...extra }).querySelector<HTMLElement>("[data-part='how']")!;
    const how = render(answer, { carSince: "2026-11-10" });
    expect(how.querySelector("h4")?.textContent).toBe("Så räknas det");
    const lines = Array.from(how.querySelectorAll("p")).map((line) => line.textContent ?? "");
    expect(lines[0]).toBe("Siffrorna visar vad som mättes och vad planerna skulle ha gjort.");
    expect(lines).toContain("Det SpotNav sparade jämförs med:");
    // The battery ran under its own control in this month: said, and no line for SpotNav's own battery part.
    expect(lines.some((line) => line.startsWith("Batteri: det styrdes av sig självt"))).toBe(true);
    expect(lines.some((line) => line.startsWith("Batteri: det batteriet tjänade medan SpotNav styrde det."))).toBe(false);
    expect(lines.some((line) => line.startsWith("Bil: att ladda direkt vid inkoppling") && line.endsWith("Bilen räknas från tis 10 nov."))).toBe(true);
    expect(lines.some((line) => line.startsWith("Värme (uppskattat)"))).toBe(true);
    expect(how.querySelector("[data-line='flow-rule']")?.textContent).toMatch(/varje (timme|kvart) går solen/);
    // Under SpotNav's control the battery's line is its own; without the car and the heat, no line for them.
    const controlled = render({ ...answer, own_control: null, saving: { ...answer.saving!, battery: 500, car: null, heat: null } });
    const own = Array.from(controlled.querySelectorAll("p")).map((line) => line.textContent ?? "");
    expect(own).toContain("Batteri: det batteriet tjänade medan SpotNav styrde det.");
    expect(own.some((line) => line.startsWith("Bil:") || line.startsWith("Värme (uppskattat)"))).toBe(false);
    // No figure of the period stands here, and nothing of the last week.
    expect(how.textContent).not.toMatch(/\d kr/);
    // Without anything SpotNav saved and without a flow: the one sentence.
    const bare = render({ ...answer, saving: null, own_control: null, energy_flow: null });
    expect(Array.from(bare.querySelectorAll("p"))).toHaveLength(1);
    for (const language of LANGUAGES) {
      const text = houseHistoryBody({ doc: document, language, state: { kind: "ready", answer }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" }).textContent ?? "";
      expect(text, language).not.toMatch(/hist\.|perfect\.|house\.|\{\w+\}/);
    }
  });

  it("says on a single day that what was left at midnight counted", () => {
    const answer = house("house_history/villa_day.json");
    const body = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui({ period: "day" }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelector("[data-part='perfect'] [data-line='perfect-midnight']")?.textContent).toBe("Räknat med det som fanns kvar i batteriet vid midnatt.");
    // An answer before the perfect plan has none.
    const old = { ...answer, review: { ...answer.review!, perfect: null } };
    expect(houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: old }, ui: ui({ period: "day" }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" }).querySelector("[data-part='perfect']")).toBeNull();
  });

  it("says what is missing: no meters, no prices, no review", () => {
    const answer = house("house_history/house_alone_year.json");
    const body = houseHistoryBody({ doc: document, language: "en", state: { kind: "ready", answer }, ui: ui({ period: "year" }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelectorAll(".spotnav-history-bar")).toHaveLength(12);
    expect(body.querySelector("[data-line='no-review']")).not.toBeNull();
    const bare = { ...answer, energy_flow: null, buckets: [], cost: null, money: null };
    const empty = houseHistoryBody({ doc: document, language: "en", state: { kind: "ready", answer: bare }, ui: ui({ period: "year" }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(empty.querySelector("[data-line='no-meters']")).not.toBeNull();
    expect(empty.querySelector("[data-line='no-cost']")?.textContent).toBe(translate("en", "hist.house.noPriceSource"));
  });

  it("draws a day of 25 hours with 25 bars", () => {
    const answer = house("house_history/villa_day.json");
    const keys = [...answer.buckets.map((bucket) => bucket.key)];
    keys.splice(3, 0, "2026-10-25T02:00:00+01:00");
    const long = { ...answer, buckets: keys.map((key, index) => ({ ...answer.buckets[Math.min(index, answer.buckets.length - 1)]!, key })) };
    const body = houseHistoryBody({ doc: document, language: "en", state: { kind: "ready", answer: long }, ui: ui({ period: "day" }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelectorAll(".spotnav-history-bar")).toHaveLength(25);
  });
});

describe("the heat history", () => {
  it("decodes every fixture", () => {
    for (const name of ["villa_month.json", "villa_day.json", "meter_only_year.json", "zones_day.json", "zones_month.json", "zones_year.json"]) {
      expect(decodeHeatHistory(read(`heat_history/${name}`)).ok).toBe(true);
    }
    expect(decodeHeatHistory({ ...read("heat_history/villa_day.json"), runs: [{ start: "x", end: "y", kwh: 1, cause: null }] }).ok).toBe(false);
  });

  it("reads the use by what is measured, SpotNav's saving and the house model, and an older answer without them", () => {
    const raw = read("heat_history/zones_month.json");
    const answer = heat("heat_history/zones_month.json");
    expect(answer.parts.map((part) => [part.key, part.kind, part.name])).toEqual([
      ["pump", "pump", null],
      ["hot_water", "hot_water", null],
      ["zone:climate.ovanvaning", "zone", "Ovanvåning"],
      ["other", "other", null],
    ]);
    expect(answer.saving).toEqual({ value: 456.9, days: 3 });
    expect(answer.model).toEqual({ mae_kw: 0.32, days: 7 });
    const day = answer.buckets.find((bucket) => bucket.key === "2026-11-13")!;
    expect(day.saving).toBe(212.4);
    expect(day.parts.reduce((sum, part) => sum + part.kwh, 0)).toBeCloseTo(day.kwh!, 2);
    const { parts: _parts, saving: _saving, model: _model, ...older } = raw;
    const old = decodeHeatHistory({ ...older, buckets: (raw["buckets"] as Record<string, unknown>[]).map(({ parts: _p, saving: _s, ...bucket }) => bucket) });
    expect(old.ok && old.value.parts).toEqual([]);
    expect(old.ok && old.value.saving).toBeNull();
    expect(old.ok && old.value.buckets[0]!.parts).toEqual([]);
    expect(decodeHeatHistory({ ...raw, parts: [{ key: "x", kind: "boat", name: null, kwh: 1, cost: null }] }).ok).toBe(false);
    expect(decodeHeatHistory({ ...raw, saving: { value: "1", days: 1 } }).ok).toBe(false);
  });

  it("says how much of the heat the zones' own meters hold and from which day", () => {
    const answer = heat("heat_history/villa_month.json");
    const plain = heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(plain.querySelector("[data-row='zones']")).toBeNull();
    const zoned = { ...answer, energy: { ...answer.energy!, zones_kwh: 26.4, zones_from: "2026-11-05" } };
    const body = heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: zoned }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelector("[data-part='use'] [data-row='zones']")?.textContent).toContain("26,4 kWh");
    expect(body.querySelector("[data-line='zones-from']")?.textContent).toContain(translate("sv", "hist.heat.zonesFrom", { date: "" }).split("{date}")[0]!.slice(0, 20));
  });

  it("shows a month whole, nothing behind a switch: the use, degree-days with the trend, comfort, the shadow and the house model", () => {
    const answer = heat("heat_history/villa_month.json");
    const body = heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelector("[data-part='use'] [data-row='cost']")).not.toBeNull();
    expect(body.querySelector("button[data-detailed]")).toBeNull();
    // The card's own period picker, as the house card's details have it.
    expect(body.querySelector(".spotnav-house-picker")).not.toBeNull();
    expect(body.querySelector("button[data-kind='month']")!.getAttribute("aria-pressed")).toBe("true");
    // The day's chart is the card's Plan tab, not drawn here.
    expect(body.querySelector("[data-block='day']")).toBeNull();
    // How well the house model forecasts the heat.
    expect(body.querySelector("[data-part='model'] [data-row='model-error']")?.textContent).toBe("Värmeprognosens fel±0,32 kW (7 d)");
    const unreviewed = heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: { ...answer, model: null } }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(unreviewed.querySelector("[data-part='model'] [data-line='no-model']")).not.toBeNull();
    // The cost bars stand only for their hatched part: what SpotNav's plan would have saved.
    expect(body.querySelector(".spotnav-history-bars, .spotnav-history-chart")).not.toBeNull();
    const plainCost = { ...answer, buckets: answer.buckets.map((bucket) => ({ ...bucket, shadow_value: null })) };
    const without = heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: plainCost }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(without.querySelectorAll(".spotnav-history-bar").length).toBe(12);
    expect(body.querySelector("[data-part='use'] [data-row='hot-water']")?.textContent).toContain("16,8 kWh");
    const degree = body.querySelector("[data-part='degree-days']")!;
    expect(degree.querySelector("[data-row='per-degree-day']")?.textContent).toContain("3,07 kWh/graddag");
    expect(degree.querySelectorAll(".spotnav-history-bar")).toHaveLength(12);
    expect(body.querySelector("[data-part='comfort'] [data-row='coldest']")?.textContent).toContain("Övervåning");
    expect(body.querySelector("[data-part='comfort'] [data-row='coldest-figure']")?.textContent).toMatch(/^medel .* · lägst /);
    // The split note only while days are missing from it.
    expect(body.querySelector("[data-line='split-days']")).not.toBeNull();
    const whole = { ...answer, energy: { ...answer.energy!, split_days: answer.energy!.days } };
    const complete = heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: whole }, ui: ui({ detailed: true }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(complete.querySelector("[data-line='split-days']")).toBeNull();
    expect(body.querySelector("[data-part='comfort'] [data-row='below']")?.textContent).toContain("112 av 374");
    expect(body.querySelector("[data-part='shadow'] [data-row='value']")?.textContent).toContain("52 kr");
    expect(body.querySelector("[data-part='shadow'] [data-line='effect']")?.textContent).toBe("Lärt av 7 uppmätta sänkningar av reläet.");
    expect(body.querySelector("[data-part='use'] [data-row='immersion']")?.textContent).toContain("3 h");
    expect(body.querySelector("[data-part='runs']")).toBeNull();
  });

  it("lists a day's hot-water runs with their cause", () => {
    const answer = heat("heat_history/villa_day.json");
    const body = heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui({ period: "day" }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    const runs = Array.from(body.querySelectorAll("[data-part='runs'] li")).map((row) => row.textContent);
    expect(runs).toEqual(["03:00–03:10" + "0,3 kWh · stilleståndsförlust", "06:15–06:40" + "1,2 kWh · efter tappning", "07:25–07:45" + "0,9 kWh · efter tappning"]);
  });

  it("shows a day's review: the relay's real lowering beside SpotNav's plan, even without Detailed", () => {
    const answer = heat("heat_history/villa_day.json");
    const body = heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui({ period: "day" }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    const review = body.querySelector("[data-part='review']")!;
    expect(review.querySelector("[data-row='review-relay']")?.textContent).toBe("Reläet sänkte 3 h3,36 kr");
    expect(review.querySelector("[data-row='review-plan']")?.textContent).toBe("SpotNav skulle ha sänkt 3 h1,28 kr");
    expect(review.querySelector("[data-row='review-relay-comfort']")?.textContent).toContain("kallast 20,3");
    // A month has no review of its own.
    const month = heat("heat_history/villa_month.json");
    const monthBody = heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: month }, ui: ui({ detailed: true }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(monthBody.querySelector("[data-part='review']")).toBeNull();
  });

  it("says what is missing without a meter, an outdoor temperature or rooms", () => {
    const answer = heat("heat_history/meter_only_year.json");
    const bare = { ...answer, meter: null, energy: null, buckets: [], shadow: null };
    const body = heatHistoryBody({ doc: document, language: "en", state: { kind: "ready", answer: bare }, ui: ui({ period: "year", detailed: true }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelector("[data-line='no-meter']")).not.toBeNull();
    expect(body.querySelector("[data-line='no-outdoor']")).not.toBeNull();
    expect(body.querySelector("[data-line='no-rooms']")).not.toBeNull();
    // Too few degree-days: one line on when there will be a figure.
    const month = heat("heat_history/villa_month.json");
    const few = { ...month, degree_days: { ...month.degree_days!, kwh_per_degree_day: null } };
    const fewBody = heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: few }, ui: ui({ detailed: true }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    const degree = fewBody.querySelector("[data-part='degree-days']")!;
    expect(degree.querySelector("[data-line='too-few']")?.textContent).toBe("Graddagar visas när det finns ett par kalla dygn att räkna på.");
    expect(degree.querySelector("[data-row='per-degree-day']")).toBeNull();
    expect(degree.textContent).not.toContain("graddagar under");
  });

  it("opens from the heat card's i, reads the running month and then a day with its runs", async () => {
    vi.useFakeTimers({ now: Date.parse("2026-11-16T13:20:00Z") });
    defineCard();
    const messages: Record<string, unknown>[] = [];
    const hass = {
      language: "sv",
      locale: { language: "sv" },
      states: {},
      user: { is_admin: false },
      callWS: async (message: Record<string, unknown>): Promise<unknown> => {
        messages.push(message);
        if (message["type"] === "spotnav/get_heat") {
          return read("heat_card/villa.json");
        }
        if (message["type"] === "spotnav/get_heat_history") {
          return read(message["period"] === "day" ? "heat_history/villa_day.json" : "heat_history/villa_month.json");
        }
        return {};
      },
    };
    const element = document.createElement(HEAT_CARD_ELEMENT) as SpotnavHeatCard;
    element.setConfig({ type: "custom:spotnav-heat-card", site: "site_villa" });
    element.hass = hass as never;
    document.body.append(element);
    await vi.advanceTimersByTimeAsync(0);
    const root = element.shadowRoot!;
    root.querySelector<HTMLButtonElement>("button[data-history='open']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.at(-1)).toEqual({ type: "spotnav/get_heat_history", api_version: 1, site_id: "site_villa", period: "month" });
    const dialog = root.querySelector<HTMLElement>("[role='dialog']")!;
    expect(root.querySelector<HTMLButtonElement>("button[data-history='open']")!.getAttribute("aria-label")).toBe("Detaljer");
    expect(dialog.querySelector("[data-part='degree-days']")).not.toBeNull();
    dialog.querySelector<HTMLButtonElement>("button[data-kind='day']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.at(-1)).toMatchObject({ type: "spotnav/get_heat_history", period: "day" });
    expect(dialog.querySelectorAll("[data-part='runs'] li")).toHaveLength(3);
    element.remove();
    vi.useRealTimers();
  });
});

describe("the words", () => {
  it("are in every language", () => {
    const keys = Object.keys(TRANSLATIONS.en).filter((key) => key.startsWith("hist."));
    expect(keys.length).toBeGreaterThan(90);
    for (const language of LANGUAGES) {
      for (const key of keys) {
        expect(TRANSLATIONS[language][key as keyof typeof TRANSLATIONS.en], `${language} ${key}`).toBeTruthy();
      }
    }
  });
});

describe("the heat history's indoor curve", () => {
  it("refuses a mistyped or unknown key in the curve", () => {
    const raw = read("heat_history/villa_day.json");
    const day = raw["indoor_day"] as Record<string, unknown>;
    expect(decodeHeatHistory({ ...raw, indoor_day: { ...day, extra: 1 } }).ok).toBe(false);
    expect(decodeHeatHistory({ ...raw, indoor_day: { ...day, low_c: "20.5" } }).ok).toBe(false);
  });
});
