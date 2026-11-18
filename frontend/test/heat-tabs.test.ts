// The heat card's tabs (SpotNav 3.0): the tab row and what it remembers, Now's strip of the hours ahead, Plan's day
// chart on a picker of days alone, Use's bars by what is measured in kWh or money, the locked tab and the box of
// fixed height.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { HEAT_CARD_ELEMENT, heatConfigForm, parseHeatConfig, SpotnavHeatCard } from "../src/heat-card";
import { decodeHeatHistory, type HeatHistory } from "../src/heat-history";
import { decodeHeat, type HeatAnswer } from "../src/heat-model";
import {
  fitHeatTabs,
  HEAT_TABS,
  heatTabForKey,
  heatTabList,
  readHeatTab,
  readUseHidden,
  readUseUnit,
  writeHeatTab,
  writeUseHidden,
  writeUseUnit,
} from "../src/heat-tabs";
import { hasMoney, partSeries, useHeadline, useNotes, usePanel, useTotal } from "../src/heat-use";
import { aheadLine, heatBody, planChoice, type HeatHandlers, type HeatUi } from "../src/heat-view";
import { periodPicker } from "../src/house-tabs";
import { LANGUAGES, registerTranslation } from "../src/i18n";
import { TRANSLATIONS } from "../src/i18n/all";
import { defineCard } from "../src/index";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function read(path: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, path), "utf8")) as Record<string, unknown>;
}

function answer(name = "villa.json"): HeatAnswer {
  const decoded = decodeHeat(read(`heat_card/${name}`));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

function history(name: string): HeatHistory {
  const decoded = decodeHeatHistory(read(`heat_history/${name}`));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

/** The fixtures' moment: 16 November 2026, 17:20 in Stockholm. */
const NOW = Date.parse("2026-11-16T16:20:00+00:00");

beforeAll(() => {
  registerTranslation("sv", TRANSLATIONS.sv);
});

function handlers(extra: Partial<HeatHandlers> = {}): HeatHandlers {
  return { onStep: vi.fn(), onHome: vi.fn(), onAway: vi.fn(), onSettings: vi.fn(), ...extra };
}

function body(ui: Partial<HeatUi>, value: HeatAnswer = answer(), tabs?: { history: HeatHistory | null }, extra: Partial<HeatHandlers> = {}) {
  return heatBody({
    doc: document,
    language: "sv",
    state: { kind: "ready", answer: value },
    ui: { pendingTarget: null, error: null, ...ui },
    handlers: handlers(extra),
    width: 330,
    nowMs: NOW,
    idPrefix: "t",
    ...(tabs === undefined ? {} : { tabs: { history: tabs.history === null ? null : { kind: "ready" as const, answer: tabs.history }, today: "2026-11-16" } }),
  });
}

describe("the tab row", () => {
  it("is a tablist of Now, Plan and Use with one tab in the tab order and its panel labelled", () => {
    const card = body({ tab: "plan" });
    const list = card.querySelector<HTMLElement>("[role='tablist']")!;
    expect(list.getAttribute("aria-label")).toBe("Värmens vyer");
    const tabs = [...list.querySelectorAll<HTMLElement>("[role='tab']")];
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Nu", "Plan", "Förbrukning"]);
    expect(tabs.map((tab) => tab.getAttribute("aria-selected"))).toEqual(["false", "true", "false"]);
    expect(tabs.map((tab) => tab.tabIndex)).toEqual([-1, 0, -1]);
    const panel = card.querySelector<HTMLElement>("[role='tabpanel']")!;
    expect(panel.id).toBe(tabs[1]!.getAttribute("aria-controls"));
    expect(panel.getAttribute("aria-labelledby")).toBe(tabs[1]!.id);
    expect(card.dataset["tab"]).toBe("plan");
  });

  it("moves with the arrow keys, Home and End, wrapping, and a click", () => {
    expect(heatTabForKey("ArrowRight", "use")).toBe("now");
    expect(heatTabForKey("ArrowLeft", "now")).toBe("use");
    expect(heatTabForKey("Home", "use")).toBe("now");
    expect(heatTabForKey("End", "now")).toBe("use");
    expect(heatTabForKey("a", "now")).toBeNull();
    const onSelect = vi.fn();
    const list = heatTabList(document, "sv", "now", "t", onSelect);
    const tabs = [...list.querySelectorAll<HTMLElement>("[role='tab']")];
    tabs[0]!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(onSelect).toHaveBeenLastCalledWith("plan", true);
    tabs[2]!.click();
    expect(onSelect).toHaveBeenLastCalledWith("use", false);
    tabs[0]!.click();
    expect(onSelect).toHaveBeenCalledTimes(2);
  });

  it("falls back to icons where the names do not fit, the names kept for a screen reader and as tooltips", () => {
    const list = heatTabList(document, "sv", "now", "t", vi.fn());
    document.body.append(list);
    Object.defineProperty(list, "clientWidth", { configurable: true, get: () => 120 });
    Object.defineProperty(list, "scrollWidth", { configurable: true, get: () => (list.dataset["fit"] === "long" ? 200 : 110) });
    expect(fitHeatTabs(list, "sv")).toBe("icons");
    const tabs = [...list.querySelectorAll<HTMLElement>("[role='tab']")];
    expect(tabs.every((tab) => tab.querySelector("svg") !== null && tab.textContent === "")).toBe(true);
    expect(tabs.map((tab) => tab.getAttribute("aria-label"))).toEqual(["Nu", "Plan", "Förbrukning"]);
    expect(tabs.map((tab) => tab.title)).toEqual(["Nu", "Plan", "Förbrukning"]);
    Object.defineProperty(list, "clientWidth", { configurable: true, get: () => 300 });
    list.dataset["fit"] = "";
    expect(fitHeatTabs(list, "sv")).toBe("long");
    expect(tabs[2]!.textContent).toBe("Förbrukning");
    expect(tabs[2]!.hasAttribute("title")).toBe(false);
    list.remove();
  });

  it("remembers the tab, the unit and the hidden parts per card, and forgets nothing when storage fails", () => {
    const stored = new Map<string, string>();
    const store = () => ({ getItem: (key: string) => stored.get(key) ?? null, setItem: (key: string, value: string) => void stored.set(key, value) });
    expect(readHeatTab("site", store)).toBeNull();
    writeHeatTab("site", "use", store);
    writeUseUnit("site", "money", store);
    writeUseHidden("site", ["hot_water", "zone:climate.ovanvaning"], store);
    expect(Object.fromEntries(stored)).toEqual({
      "spotnav-heat-card.tab.site": "use",
      "spotnav-heat-card.use.unit.site": "money",
      "spotnav-heat-card.use.hidden.site": '["hot_water","zone:climate.ovanvaning"]',
    });
    expect(readHeatTab("site", store)).toBe("use");
    expect(readHeatTab("other", store)).toBeNull();
    expect(readUseUnit("site", store)).toBe("money");
    expect(readUseUnit("other", store)).toBe("kwh");
    expect(readUseHidden("site", store)).toEqual(["hot_water", "zone:climate.ovanvaning"]);
    stored.set("spotnav-heat-card.tab.site", "flow");
    stored.set("spotnav-heat-card.use.hidden.site", "{not json");
    expect(readHeatTab("site", store)).toBeNull();
    expect(readUseHidden("site", store)).toEqual([]);
    stored.set("spotnav-heat-card.use.hidden.site", '[1, "pump", null]');
    expect(readUseHidden("site", store)).toEqual(["pump"]);
    const broken = () => {
      throw new Error("blocked");
    };
    expect(readHeatTab("site", broken)).toBeNull();
    expect(readUseUnit("site", broken)).toBe("kwh");
    expect(readUseHidden("site", broken)).toEqual([]);
    expect(() => writeHeatTab("site", "now", broken)).not.toThrow();
    expect(() => writeUseUnit("site", "kwh", () => null)).not.toThrow();
  });

  it("words its tabs in every language", () => {
    for (const language of LANGUAGES) {
      for (const key of ["heat.tabs", ...HEAT_TABS.map((tab) => `heat.tab.${tab}`)]) {
        expect(TRANSLATIONS[language][key as keyof typeof TRANSLATIONS.en], `${language} ${key}`).toBeTruthy();
      }
    }
  });
});

describe("the card's YAML and editor", () => {
  it("takes a tab to lock, refuses an unknown one, and offers the tabs in the editor", () => {
    expect(parseHeatConfig({ site: "s", tab: "use" }).tab).toBe("use");
    expect(parseHeatConfig({ site: "s", tab: "all" }).tab).toBeUndefined();
    expect(parseHeatConfig({ site: "s", tab: "" }).tab).toBeUndefined();
    expect(() => parseHeatConfig({ site: "s", tab: "flow" })).toThrow("tab must be one of now, plan, use");
    const form = heatConfigForm("sv");
    expect(JSON.stringify(form.schema)).toContain('"options":[{"value":"all","label":"(alla)"},{"value":"now","label":"Nu"},{"value":"plan","label":"Plan"},{"value":"use","label":"Förbrukning"}]');
    expect(form.computeLabel({ name: "tab" })).toBe("Visa bara flik");
    expect(typeof SpotnavHeatCard.getConfigForm().computeLabel).toBe("function");
  });

  it("draws a locked tab without the tab row", () => {
    const card = body({ tab: "now", locked: "plan" });
    expect(card.querySelector("[role='tablist']")).toBeNull();
    expect(card.dataset["tab"]).toBe("plan");
    const panel = card.querySelector<HTMLElement>(".spotnav-house-panel")!;
    expect(panel.hasAttribute("role")).toBe(false);
    expect(panel.querySelector("[data-block='day']")).not.toBeNull();
  });
});

describe("Now", () => {
  it("has the dial, home or away, the lines, the strip of the hours ahead and today, and no chart", () => {
    const card = body({ tab: "now" });
    const panel = card.querySelector<HTMLElement>("[data-tab='now'].spotnav-house-panel")!;
    expect(panel.querySelector(".spotnav-heat-dial")).not.toBeNull();
    expect(panel.querySelector("button[data-mode='home']")).not.toBeNull();
    expect(panel.querySelector("[data-line='would']")).not.toBeNull();
    expect(panel.querySelector("[data-line='today']")!.textContent).toContain("I dag · värme 17,5 kWh");
    expect(panel.querySelector("[data-block='day']")).toBeNull();
    expect(panel.querySelector(".spotnav-house-picker")).toBeNull();
  });

  it("draws twelve hours ahead, an open cell where tomorrow is not planned yet, and says the plan in words", () => {
    const villa = answer();
    expect(villa.ahead).toHaveLength(7);
    expect(villa.ahead[0]!.startMs).toBe(Date.parse("2026-11-16T16:00:00+00:00"));
    const strip = body({ tab: "now" }, villa).querySelector<HTMLElement>("[data-block='ahead']")!;
    const cells = [...strip.querySelectorAll<HTMLElement>(".spotnav-heat-ahead-cell")];
    expect(cells).toHaveLength(12);
    expect(cells.map((cell) => cell.dataset["action"])).toEqual(["lower", "lower", "normal", "lower", "normal", "normal", "night", "open", "open", "open", "open", "open"]);
    expect([...strip.querySelectorAll(".spotnav-heat-ahead-axis span")].map((item) => item.textContent)).toEqual(["17:00", "20:00", "23:00", "02:00"]);
    expect(strip.querySelector("[data-line='ahead']")!.textContent).toBe("Plan: sänkning 17:00–19:00 · svalare natt 23:00–00:00");
    expect(aheadLine("en", villa)).toBe("Plan: lowering 17:00–19:00 · cooler night 23:00–00:00");
  });

  it("names a pre-heat, a raise through SG Ready, hot water and a plan that lowers nothing", () => {
    const villa = answer();
    const hours = villa.ahead.map((hour, index) => ({ ...hour, action: index === 1 ? "preheat" : "normal", hot_water: index === 4 }));
    const planned = { ...villa, ahead: hours };
    expect(aheadLine("sv", planned)).toBe("Plan: förvärmning 18:00–19:00 · varmvatten 21:00");
    const sgReady = { ...planned, config: { ...villa.config, control_path: "sg_ready", control_entity: "switch.a", control_entity_b: "switch.b" } };
    expect(aheadLine("sv", sgReady)).toBe("Plan: höjning 18:00–19:00 · varmvatten 21:00");
    const strip = body({ tab: "now" }, sgReady).querySelector<HTMLElement>("[data-block='ahead']")!;
    const cells = [...strip.querySelectorAll<HTMLElement>(".spotnav-heat-ahead-cell")];
    expect(cells[1]!.dataset["action"]).toBe("raise");
    expect(cells[4]!.dataset["water"]).toBe("true");
    const calm = { ...villa, ahead: villa.ahead.map((hour) => ({ ...hour, action: "normal", hot_water: false })) };
    expect(aheadLine("sv", calm)).toBe("Ingen sänkning planerad de närmaste 12 timmarna");
    expect(aheadLine("sv", { ...villa, ahead: [] })).toBeNull();
    expect(body({ tab: "now" }, { ...villa, ahead: [] }).querySelector("[data-block='ahead']")).toBeNull();
  });

  it("is drawn compactly in a box: the dial at the height given, no explaining line and no notes", () => {
    const plain = body({ tab: "now" });
    expect(plain.querySelector("[data-line='shadow']")).not.toBeNull();
    expect(plain.querySelectorAll("[data-note]").length).toBeGreaterThan(0);
    const boxed = body({ tab: "now", fill: 240 });
    expect(boxed.querySelector("[data-line='shadow']")).toBeNull();
    expect(boxed.querySelectorAll("[data-note]")).toHaveLength(0);
    const dial = boxed.querySelector<SVGElement>(".spotnav-heat-dial")!;
    expect(dial.style.height).toBe("240px");
    expect(dial.style.maxWidth).toBe("282px");
    expect(boxed.querySelector("[data-block='ahead']")).not.toBeNull();
  });
});

describe("Plan", () => {
  it("shows today's chart under a picker of days alone, with yesterday's review", () => {
    const card = body({ tab: "plan" });
    const picker = card.querySelector<HTMLElement>(".spotnav-house-picker")!;
    expect(picker.querySelector("[data-pick-label]")!.textContent).toBe("I dag");
    expect(picker.querySelectorAll("button[data-kind]")).toHaveLength(0);
    expect(picker.querySelector<HTMLButtonElement>("button[data-step='forward']")!.disabled).toBe(true);
    expect(card.querySelector("[data-block='band'] [data-block='day'] .spotnav-heat-day-now")).not.toBeNull();
    expect(card.querySelector("[data-line='review-relay']")).not.toBeNull();
    expect(card.querySelector(".spotnav-heat-dial")).toBeNull();
  });

  it("keeps its own day while the shared period is a month, and tells the card a day chosen there is Plan's", () => {
    expect(planChoice({ pendingTarget: null, error: null, choice: { period: "day", date: "2026-11-14" } })).toEqual({ period: "day", date: "2026-11-14" });
    expect(planChoice({ pendingTarget: null, error: null, choice: { period: "month", date: "2026-10-01" } })).toEqual({ period: "day", date: null });
    expect(planChoice({ pendingTarget: null, error: null, choice: { period: "year", date: null }, planDate: "2026-11-12" })).toEqual({ period: "day", date: "2026-11-12" });
    const onPeriod = vi.fn();
    const card = body({ tab: "plan", choice: { period: "month", date: null } }, answer(), { history: null }, { onPeriod });
    expect(card.querySelector("[data-pick-label]")!.textContent).toBe("I dag");
    card.querySelector<HTMLButtonElement>("button[data-step='back']")!.click();
    expect(onPeriod).toHaveBeenLastCalledWith({ period: "day", date: "2026-11-15" }, true);
  });

  it("draws a past day from its history: the rooms by the hour, the setpoint and the hot-water runs", () => {
    const day = history("zones_day.json");
    const card = body({ tab: "plan", choice: { period: "day", date: "2026-11-15" } }, answer(), { history: day });
    expect(card.querySelector("[data-pick-label]")!.textContent).toBe("I går");
    const chart = card.querySelector<HTMLElement>("[data-block='day']")!;
    expect(chart.querySelector("[data-line='mean']")).not.toBeNull();
    expect(chart.querySelectorAll("[data-mark='hot_water']")).toHaveLength(day.runs.length);
    expect(chart.querySelector(".spotnav-heat-day-now")).toBeNull();
    expect(card.querySelector("[data-line='review-relay']")).toBeNull();
    // On its way, unreadable, and a day without rooms.
    const loading = body({ tab: "plan", choice: { period: "day", date: "2026-11-15" } }, answer(), { history: null });
    expect(loading.querySelector("[data-line='loading']")!.getAttribute("aria-busy")).toBe("true");
    const empty = body({ tab: "plan", choice: { period: "day", date: "2026-11-15" } }, answer(), { history: { ...day, indoor_day: null } });
    expect(empty.querySelector("[data-line='no-day']")!.textContent).toBe("Inget att visa för den här dagen.");
  });

  it("gives the chart the box's height", () => {
    const card = body({ tab: "plan", fill: 300 });
    expect(card.querySelector(".spotnav-heat-day")!.getAttribute("viewBox")).toBe("0 0 330 300");
    expect(card.querySelector("[data-line='review-relay']")).toBeNull();
    expect(body({ tab: "plan" }).querySelector(".spotnav-heat-day")!.getAttribute("viewBox")).toBe("0 0 330 140");
  });
});

describe("the period picker", () => {
  it("leaves the kinds out when only one is offered, and keeps all three otherwise", () => {
    const one = periodPicker(document, "sv", { period: "day", date: null }, "2026-11-16", vi.fn(), null, ["day"]);
    expect(one.querySelectorAll("button[data-kind]")).toHaveLength(0);
    expect(one.querySelector("input[data-pick='day']")).not.toBeNull();
    const all = periodPicker(document, "sv", { period: "day", date: null }, "2026-11-16", vi.fn());
    expect([...all.querySelectorAll<HTMLElement>("button[data-kind]")].map((button) => button.dataset["kind"])).toEqual(["day", "month", "year"]);
  });
});

describe("Use", () => {
  const month = (): HeatHistory => history("zones_month.json");

  function panel(answerOf: HeatHistory, ui: { hidden?: string[]; unit?: "kwh" | "money"; boxed?: boolean } = {}, on: { onUnit?: (unit: "kwh" | "money") => void; onToggle?: (hidden: string[]) => void } = {}) {
    const wrap = document.createElement("div");
    wrap.append(
      ...usePanel({
        doc: document,
        language: "sv",
        answer: answerOf,
        hidden: ui.hidden ?? [],
        unit: ui.unit ?? "kwh",
        height: 150,
        selected: null,
        boxed: ui.boxed ?? false,
        onUnit: on.onUnit ?? vi.fn(),
        onToggle: on.onToggle ?? vi.fn(),
      }),
    );
    return wrap;
  }

  it("heads the period with what SpotNav saved on heat, the total under it, and the parts as toggles with their sums", () => {
    const wrap = panel(month());
    expect(wrap.querySelector("[data-line='use-saved']")!.textContent).toBe("SpotNav sparade 4,57 kr på värmen");
    expect(wrap.querySelector("[data-line='use-total']")!.textContent).toBe("Värme 665 kWh");
    const legend = [...wrap.querySelectorAll<HTMLButtonElement>("button[data-legend]")];
    expect(legend.map((button) => button.textContent)).toEqual(["Värmepump 581 kWh", "Varmvatten 16,8 kWh", "Ovanvåning 48,7 kWh", "Övrig värme 18,7 kWh"]);
    expect(legend.every((button) => button.getAttribute("aria-pressed") === "true")).toBe(true);
    expect([...wrap.querySelectorAll<HTMLElement>(".spotnav-heat-use-swatch")].map((swatch) => swatch.dataset["series"])).toEqual(["use-pump", "use-hot_water", "use-zone-0", "use-other"]);
    // Without a saving the total is the headline.
    const none = panel({ ...month(), saving: null });
    expect(none.querySelector("[data-line='use-saved']")).toBeNull();
    expect(none.querySelector("[data-line='use-total']")!.textContent).toBe("Värme 665 kWh");
    expect(useHeadline("sv", { ...month(), saving: null }, new Set(), "kwh")).toEqual({ head: "Värme 665 kWh", sub: null, saved: false });
  });

  it("stacks a bar per day by part and says a bar's figures, the day's saving with them", () => {
    const wrap = panel(month());
    const bars = [...wrap.querySelectorAll<HTMLButtonElement>("[data-part='use-bars'] button[data-bar]")];
    expect(bars).toHaveLength(30);
    const day = bars.find((bar) => bar.dataset["bar"] === "2026-11-13")!;
    expect([...day.querySelectorAll<HTMLElement>(".spotnav-house-pbar-part")].map((part) => part.dataset["series"])).toEqual(["use-other", "use-zone-0", "use-hot_water", "use-pump"]);
    expect(day.getAttribute("aria-label")).toMatch(/^fre 13 nov\.?: 4\d,\d kWh · Värmepump 3\d,\d kWh · Varmvatten 2,4 kWh · Ovanvåning 3,\d kWh · Övrig värme 1,2 kWh · SpotNav sparade 2,12 kr$/);
    // A day ahead has nothing to show.
    const ahead = bars.find((bar) => bar.dataset["bar"] === "2026-11-20")!;
    expect(ahead.querySelector(".spotnav-house-pbar-lane")).toBeNull();
    expect(ahead.getAttribute("aria-label")).toMatch(/: ingen mätaravläsning$/);
    // A day has an hour per bar, the hot water in the hours it ran.
    const hours = [...panel(history("zones_day.json")).querySelectorAll<HTMLButtonElement>("button[data-bar]")];
    expect(hours).toHaveLength(24);
    expect(hours.filter((bar) => bar.querySelector("[data-series='use-hot_water']") !== null)).toHaveLength(3);
  });

  it("hides a part: out of the bars and the total, kept as a toggle; with every part hidden it says so", () => {
    const onToggle = vi.fn();
    const wrap = panel(month(), { hidden: ["pump", "gone"] }, { onToggle });
    expect(wrap.querySelector("[data-line='use-total']")!.textContent).toBe("Värme 84,2 kWh");
    expect(wrap.querySelector("[data-part='use-bars'] [data-series='use-pump']")).toBeNull();
    const pump = wrap.querySelector<HTMLButtonElement>("button[data-legend='use-pump']")!;
    expect(pump.getAttribute("aria-pressed")).toBe("false");
    pump.click();
    // A part the answer no longer has is not kept.
    expect(onToggle).toHaveBeenLastCalledWith([]);
    wrap.querySelector<HTMLButtonElement>("button[data-legend='use-other']")!.click();
    expect(onToggle).toHaveBeenLastCalledWith(["pump", "other"]);
    const all = month().parts.map((part) => part.key);
    const nothing = panel(month(), { hidden: all });
    expect(nothing.querySelector("[data-line='use-total']")!.textContent).toBe("Ingen del vald.");
    expect(nothing.querySelector("[data-part='use-bars']")).toBeNull();
    expect(useTotal(month(), new Set(all), "kwh")).toBeNull();
  });

  it("switches between kWh and money, and has no switch without a priced hour", () => {
    const onUnit = vi.fn();
    const wrap = panel(month(), {}, { onUnit });
    const units = [...wrap.querySelectorAll<HTMLButtonElement>("button[data-unit]")];
    expect(units.map((button) => [button.textContent, button.getAttribute("aria-pressed")])).toEqual([["kWh", "true"], ["kr", "false"]]);
    expect(units[1]!.getAttribute("aria-label")).toBe("Kostnad i kr");
    expect(units[1]!.parentElement!.getAttribute("role")).toBe("group");
    units[0]!.click();
    expect(onUnit).not.toHaveBeenCalled();
    units[1]!.click();
    expect(onUnit).toHaveBeenLastCalledWith("money");
    const money = panel(month(), { unit: "money" });
    expect(money.querySelector("[data-line='use-total']")!.textContent).toBe("Värme 982 kr");
    expect(money.querySelector("button[data-legend='use-hot_water']")!.textContent).toBe("Varmvatten 22 kr");
    expect(money.querySelector<HTMLElement>("[data-part='use-bars']")!.dataset["unit"]).toBe("money");
    expect(money.querySelector("button[data-bar='2026-11-13']")!.getAttribute("aria-label")).toMatch(/: \d\d kr · Värmepump \d\d kr · Varmvatten \d,\d\d kr · /);
    expect(money.querySelector("[data-line='use-notes']")!.textContent).toContain("Kostnader är till sparade priser med dagens avgifter.");
    const unpriced: HeatHistory = { ...month(), parts: month().parts.map((part) => ({ ...part, cost: null })) };
    expect(hasMoney(unpriced)).toBe(false);
    const plain = panel(unpriced, { unit: "money" });
    expect(plain.querySelector("button[data-unit]")).toBeNull();
    expect(plain.querySelector<HTMLElement>("[data-part='use-bars']")!.dataset["unit"]).toBe("kwh");
  });

  it("says which days the hot water is split out for, colours each zone its own, and what is missing", () => {
    expect(useNotes("sv", month(), "kwh")).toEqual(["Varmvatten är utbrutet för 7 av 16 dygn: de dygn Home Assistants historik ännu har i 5-minutersdetalj."]);
    expect(useNotes("sv", history("zones_day.json"), "kwh")).toEqual([]);
    expect(panel(month(), { boxed: true }).querySelector("[data-line='use-notes']")).toBeNull();
    const zones = [0, 1, 2, 3, 4].map((index) => ({ key: `zone:${index}`, kind: "zone" as const, name: `Zon ${index}`, kwh: 1, cost: null }));
    expect(zones.map((zone) => partSeries(zones, zone))).toEqual(["use-zone-0", "use-zone-1", "use-zone-2", "use-zone-3", "use-zone-0"]);
    expect(panel({ ...month(), meter: null }).querySelector("[data-line='no-meter']")).not.toBeNull();
    expect(panel({ ...month(), parts: [] }).querySelector("[data-line='no-energy']")).not.toBeNull();
  });

  it("sits under the shared picker in the card", () => {
    const onPeriod = vi.fn();
    const card = body({ tab: "use", choice: { period: "month", date: null } }, answer(), { history: month() }, { onPeriod });
    expect(card.querySelector("[data-pick-label]")!.textContent).toBe("nov 2026");
    expect(card.querySelectorAll("button[data-kind]")).toHaveLength(3);
    card.querySelector<HTMLButtonElement>("button[data-kind='year']")!.click();
    expect(onPeriod).toHaveBeenLastCalledWith({ period: "year", date: null }, false);
    expect(card.querySelector("[data-part='use-bars']")).not.toBeNull();
  });
});

describe("the element", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: NOW });
    defineCard();
  });
  afterEach(() => {
    vi.useRealTimers();
    document.body.replaceChildren();
  });

  function mount(config: Record<string, unknown> = {}, stored = new Map<string, string>()) {
    const messages: Record<string, unknown>[] = [];
    const hass = {
      language: "sv",
      locale: { language: "sv" },
      states: {},
      user: { is_admin: true },
      callWS: async (message: Record<string, unknown>): Promise<unknown> => {
        messages.push(message);
        if (message["type"] === "spotnav/get_heat") {
          return read("heat_card/villa.json");
        }
        if (message["type"] === "spotnav/get_heat_history") {
          return read(`heat_history/zones_${String(message["period"])}.json`);
        }
        return {};
      },
    };
    const element = document.createElement(HEAT_CARD_ELEMENT) as SpotnavHeatCard;
    element.store = () => ({ getItem: (key: string) => stored.get(key) ?? null, setItem: (key: string, value: string) => void stored.set(key, value) });
    element.setConfig({ type: "custom:spotnav-heat-card", site: "site_villa", ...config });
    element.hass = hass as never;
    document.body.append(element);
    const history = () => messages.filter((message) => message["type"] === "spotnav/get_heat_history");
    return { element, messages, history, stored, root: element.shadowRoot! };
  }

  it("opens on Now, reads a period only for the tab that needs one, and remembers the tab", async () => {
    const { root, history, stored } = mount();
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector<HTMLElement>(".spotnav-heat")!.dataset["tab"]).toBe("now");
    expect(history()).toHaveLength(0);
    root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='plan']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    // Today's plan is the card's own answer.
    expect(history()).toHaveLength(0);
    expect(root.querySelector("[data-block='day']")).not.toBeNull();
    root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='use']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(history().at(-1)).toEqual({ type: "spotnav/get_heat_history", api_version: 1, site_id: "site_villa", period: "day" });
    expect(root.querySelector("[data-part='use-bars']")).not.toBeNull();
    expect(stored.get("spotnav-heat-card.tab.site_villa")).toBe("use");
  });

  it("opens on the tab remembered, in the unit and with the parts hidden as they were left", async () => {
    const stored = new Map([
      ["spotnav-heat-card.tab.site_villa", "use"],
      ["spotnav-heat-card.use.unit.site_villa", "money"],
      ["spotnav-heat-card.use.hidden.site_villa", '["other"]'],
    ]);
    const { root } = mount({}, stored);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector<HTMLElement>(".spotnav-heat")!.dataset["tab"]).toBe("use");
    expect(root.querySelector("button[data-unit='money']")!.getAttribute("aria-pressed")).toBe("true");
    expect(root.querySelector("button[data-legend='use-other']")!.getAttribute("aria-pressed")).toBe("false");
    root.querySelector<HTMLButtonElement>("button[data-unit='kwh']")!.click();
    root.querySelector<HTMLButtonElement>("button[data-legend='use-other']")!.click();
    expect(stored.get("spotnav-heat-card.use.unit.site_villa")).toBe("kwh");
    expect(stored.get("spotnav-heat-card.use.hidden.site_villa")).toBe("[]");
    expect(root.querySelector("button[data-legend='use-other']")!.getAttribute("aria-pressed")).toBe("true");
  });

  it("keeps the month chosen in Use while Plan steps through its own days", async () => {
    const { root, history } = mount();
    await vi.advanceTimersByTimeAsync(0);
    root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='use']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    root.querySelector<HTMLButtonElement>("button[data-kind='month']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(history().at(-1)).toMatchObject({ period: "month" });
    root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='plan']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[data-pick-label]")!.textContent).toBe("I dag");
    root.querySelector<HTMLButtonElement>("button[data-step='back']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(history().at(-1)).toEqual({ type: "spotnav/get_heat_history", api_version: 1, site_id: "site_villa", period: "day", date: "2026-11-15" });
    expect(root.querySelector("[data-pick-label]")!.textContent).toBe("I går");
    root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='use']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[data-pick-label]")!.textContent).toBe("nov 2026");
    expect(root.querySelector("button[data-kind='month']")!.getAttribute("aria-pressed")).toBe("true");
  });

  it("shares a day between Plan and Use while the period is a day", async () => {
    const { root } = mount();
    await vi.advanceTimersByTimeAsync(0);
    root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='plan']")!.click();
    root.querySelector<HTMLButtonElement>("button[data-step='back']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='use']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[data-pick-label]")!.textContent).toBe("I går");
  });

  it("locks a tab from the YAML: no tab row, nothing read from or written to the remembered tab", async () => {
    const stored = new Map([["spotnav-heat-card.tab.site_villa", "now"]]);
    const { root, history } = mount({ tab: "use" }, stored);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[role='tablist']")).toBeNull();
    expect(root.querySelector<HTMLElement>(".spotnav-heat")!.dataset["tab"]).toBe("use");
    expect(history()).toHaveLength(1);
    expect(stored.get("spotnav-heat-card.tab.site_villa")).toBe("now");
  });

  it("moves between the tabs with the keyboard and keeps the focus on the tab moved to", async () => {
    const { root } = mount();
    await vi.advanceTimersByTimeAsync(0);
    const now = root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='now']")!;
    now.focus();
    now.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    await vi.advanceTimersByTimeAsync(0);
    const use = root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='use']")!;
    expect(use.getAttribute("aria-selected")).toBe("true");
    expect(root.activeElement).toBe(use);
  });

  it("keeps the focus on a dial step across the redraw its tap makes", async () => {
    const { root } = mount();
    await vi.advanceTimersByTimeAsync(0);
    const up = root.querySelector<HTMLButtonElement>("button[data-step='up']")!;
    up.focus();
    up.click();
    const again = root.querySelector<HTMLButtonElement>("button[data-step='up']")!;
    expect(again).not.toBe(up);
    expect(root.activeElement).toBe(again);
  });
});
