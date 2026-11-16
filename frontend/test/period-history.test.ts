// The house and heat cards' history (`house-history.ts`, `heat-history.ts`, `period-history.ts`): the strict decoders on
// the backend's own fixtures, the period steps and pickers, the Detailed switch remembered per browser, and what each
// period shows, including a day of 25 hours and the heat card's hot-water runs.

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
    for (const name of ["villa_month.json", "villa_day.json", "house_alone_year.json"]) {
      expect(decodeHouseHistory(read(`house_history/${name}`)).ok).toBe(true);
    }
    expect(decodeHouseHistory({ ...read("house_history/villa_month.json"), api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    expect(decodeHouseHistory({ ...read("house_history/villa_month.json"), period: "week" })).toEqual({ ok: false, failure: "malformed" });
    // An added key is fine.
    expect(decodeHouseHistory({ ...read("house_history/villa_month.json"), later: 1 }).ok).toBe(true);
  });

  it("shows a month: a bar a day, the flow, the money; no forward step past today", () => {
    const answer = house("house_history/villa_month.json");
    const on = handlers();
    const body = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui(), handlers: on, width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelectorAll(".spotnav-history-bar")).toHaveLength(30);
    expect(body.querySelector("button[data-step='forward']")!.hasAttribute("disabled")).toBe(true);
    expect(body.querySelector<HTMLSelectElement>("select[data-pick='month']")!.value).toBe("2026-11-01");
    expect(body.querySelector("[data-part='energy-flow'] svg")).not.toBeNull();
    const money = body.querySelector("[data-part='money']")!;
    expect(money.querySelector("[data-row='cost']")?.textContent).toMatch(/^Vad perioden kostade/);
    expect(money.querySelector("[data-row='battery']")?.textContent).toContain("52 kr");
    expect(money.querySelector("[data-line='reviewed']")?.textContent).toBe("Batteriets pengar är från 13 av 15 granskade dygn.");
    expect(body.querySelector("[data-part='modes']")).toBeNull();
    body.querySelector<HTMLButtonElement>("button[data-step='back']")!.click();
    expect(on.onPeriod).toHaveBeenCalledWith("month", "2026-10-01");
    body.querySelector<HTMLButtonElement>("button[data-period='year']")!.click();
    expect(on.onPeriod).toHaveBeenCalledWith("year", null);
    body.querySelector<HTMLButtonElement>("button[data-detailed]")!.click();
    expect(on.onDetailed).toHaveBeenCalledWith(true);
  });

  it("shows the modes, the forecasts' errors and each day with Detailed on", () => {
    const answer = house("house_history/villa_day.json");
    const body = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui({ period: "day", detailed: true }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelectorAll(".spotnav-history-bar")).toHaveLength(24);
    expect(body.querySelector<HTMLInputElement>("input[data-pick='day']")!.value).toBe("2026-11-15");
    expect(body.querySelector<HTMLInputElement>("input[data-pick='day']")!.max).toBe("2026-11-16");
    expect(body.querySelector("[data-part='modes'] [data-row='max_profit']")?.textContent).toContain("2,00 kr");
    // Self-consumption is the zero the others are measured against, not a row.
    expect(body.querySelector("[data-part='modes'] [data-row='self_consumption']")).toBeNull();
    expect(body.querySelector("[data-part='modes'] [data-row='own']")?.textContent).toContain("Som batteriet gick");
    expect(body.querySelector("[data-part='forecast'] [data-row='house']")?.textContent).toBe("Husets last±0,42 kW (1 d)");
    // A day has no day-by-day list.
    expect(body.querySelector("[data-part='days']")).toBeNull();
    const month = houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer: house("house_history/villa_month.json") }, ui: ui({ detailed: true }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(month.querySelectorAll("[data-part='days'] li")).toHaveLength(13);
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
    for (const name of ["villa_month.json", "villa_day.json", "meter_only_year.json"]) {
      expect(decodeHeatHistory(read(`heat_history/${name}`)).ok).toBe(true);
    }
    expect(decodeHeatHistory({ ...read("heat_history/villa_day.json"), runs: [{ start: "x", end: "y", kwh: 1, cause: null }] }).ok).toBe(false);
  });

  it("shows a month's use, degree-days with the trend, comfort and the shadow", () => {
    const answer = heat("heat_history/villa_month.json");
    const body = heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui(), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelector("[data-part='use'] [data-row='hot-water']")?.textContent).toContain("16,8 kWh");
    const degree = body.querySelector("[data-part='degree-days']")!;
    expect(degree.querySelector("[data-row='per-degree-day']")?.textContent).toContain("3,07 kWh/graddag");
    expect(degree.querySelectorAll(".spotnav-history-bar")).toHaveLength(12);
    expect(body.querySelector("[data-part='comfort'] [data-row='coldest']")?.textContent).toContain("Övervåning");
    expect(body.querySelector("[data-part='comfort'] [data-row='below']")?.textContent).toContain("112 av 374");
    expect(body.querySelector("[data-part='shadow'] [data-row='value']")?.textContent).toContain("127 kr");
    expect(body.querySelector("[data-part='use'] [data-row='immersion']")?.textContent).toContain("3 h");
    expect(body.querySelector("[data-part='runs']")).toBeNull();
  });

  it("lists a day's hot-water runs with their cause when Detailed is on", () => {
    const answer = heat("heat_history/villa_day.json");
    const body = heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui: ui({ period: "day", detailed: true }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    const runs = Array.from(body.querySelectorAll("[data-part='runs'] li")).map((row) => row.textContent);
    expect(runs).toEqual(["03:00–03:10" + "0,3 kWh · stilleståndsförlust", "06:15–06:40" + "1,2 kWh · efter tappning", "07:25–07:45" + "0,9 kWh · efter tappning"]);
  });

  it("says what is missing without a meter, an outdoor temperature or rooms", () => {
    const answer = heat("heat_history/meter_only_year.json");
    const bare = { ...answer, meter: null, energy: null, buckets: [], shadow: null };
    const body = heatHistoryBody({ doc: document, language: "en", state: { kind: "ready", answer: bare }, ui: ui({ period: "year" }), handlers: handlers(), width: 330, idPrefix: "t", today: "2026-11-16" });
    expect(body.querySelector("[data-line='no-meter']")).not.toBeNull();
    expect(body.querySelector("[data-line='no-outdoor']")).not.toBeNull();
    expect(body.querySelector("[data-line='no-rooms']")).not.toBeNull();
  });

  it("opens from the heat card's clock, reads the running month and then a day with its runs", async () => {
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
    expect(dialog.querySelector("[data-part='degree-days']")).not.toBeNull();
    dialog.querySelector<HTMLButtonElement>("button[data-detailed]")!.click();
    dialog.querySelector<HTMLButtonElement>("button[data-period='day']")!.click();
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
