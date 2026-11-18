// The house card's tabs (SpotNav 3.0): the tablist and its keys, the tab remembered per card, the YAML's locked tab,
// the shared period picker and its limits, each tab's content, the live flows' decoder and older payloads.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HOUSE_CARD_ELEMENT, houseConfigForm, LIVE_REFRESH_MS, parseHouseConfig, SpotnavHouseCard } from "../src/house-card";
import { decodeHouseHistory, type HouseHistory } from "../src/house-history";
import { decodeHouse, type HouseAnswer } from "../src/house-model";
import { decodeHouseNow, dotLap, fitName, nodeSpots, NOW_STYLES, patchSchematic, planRuns, planSentence, renderSchematic, schematicHeight, schematicLines, type HouseNow } from "../src/house-now";
import { earnedBars, earnedTotal, energyChart, flowTiles, niceStep, tilesBlock } from "../src/house-period-view";
import { choiceLabel, fitTabs, kindChoice, periodPicker, readTab, setTabFit, stepChoice, tabForKey, tabKey, tabList, TAB_STYLES, writeTab, type PeriodChoice } from "../src/house-tabs";
import { houseBody, type HouseTabsData, type HouseUi } from "../src/house-view";
import { translate } from "../src/i18n";
import { FakeHass } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
/** The house fixture's own moment: 13 November 2026, 17:20 in Stockholm. */
const NOW = Date.parse("2026-11-13T16:20:00+00:00");
const TODAY = "2026-11-13";

function read(...parts: string[]): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, unknown>;
}

function house(name = "villa_battery_sun.json"): HouseAnswer {
  const decoded = decodeHouse(read("house", name));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

function live(name = "villa_now.json"): HouseNow {
  const decoded = decodeHouseNow(read("house_now", name));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

function history(name: string): HouseHistory {
  const decoded = decodeHouseHistory(read("house_history", name));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

function render(answer: HouseAnswer, ui: Partial<HouseUi>, tabs: Partial<HouseTabsData> = {}, language: "en" | "sv" = "en", handlers: Record<string, unknown> = {}): HTMLElement {
  return houseBody({
    doc: document,
    language,
    state: { kind: "ready", answer },
    ui: { selected: null, ...ui },
    handlers: { onSelect: vi.fn(), ...handlers } as never,
    width: 340,
    nowMs: NOW,
    idPrefix: "t",
    tabs: { live: null, history: null, day: null, today: TODAY, ...tabs },
  });
}

describe("the tablist", () => {
  it("is a WAI-ARIA tablist in a fixed order, the selected tab alone in the tab order", () => {
    const list = tabList(document, "sv", "graph", "t", vi.fn());
    expect(list.getAttribute("role")).toBe("tablist");
    const tabs = Array.from(list.querySelectorAll<HTMLButtonElement>("[role='tab']"));
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Nu", "Energiflöde", "Energigraf", "Intäkt", "Pris och batteri"]);
    expect(tabs.map((tab) => tab.getAttribute("aria-selected"))).toEqual(["false", "false", "true", "false", "false"]);
    expect(tabs.map((tab) => tab.tabIndex)).toEqual([-1, -1, 0, -1, -1]);
    expect(tabs[2]!.getAttribute("aria-controls")).toBe("t-panel-graph");
    const card = render(house(), { tab: "graph" });
    const panel = card.querySelector("[role='tabpanel']")!;
    expect(panel.id).toBe("t-panel-graph");
    expect(panel.getAttribute("aria-labelledby")).toBe("t-tab-graph");
  });

  it("words the row by the room it has: long names, short ones, then icons, the long name always its accessible name", () => {
    const list = tabList(document, "sv", "now", "t", vi.fn());
    const tabs = Array.from(list.querySelectorAll<HTMLButtonElement>("[role='tab']"));
    const names = (): Array<string | null> => tabs.map((tab) => tab.textContent);
    const long = ["Nu", "Energiflöde", "Energigraf", "Intäkt", "Pris och batteri"];
    expect(list.dataset["fit"]).toBe("long");
    expect(tabs.every((tab) => !tab.hasAttribute("title"))).toBe(true);
    // The row as laid out: long names take 420 px, short ones 250 px, icons always fit.
    const room = (width: number): void => {
      Object.defineProperty(list, "clientWidth", { configurable: true, get: () => width });
      Object.defineProperty(list, "scrollWidth", { configurable: true, get: () => Math.max(width, list.dataset["fit"] === "long" ? 420 : list.dataset["fit"] === "short" ? 250 : 180) });
    };
    room(600);
    expect(fitTabs(list, "sv")).toBe("long");
    expect(names()).toEqual(long);
    room(328);
    expect(fitTabs(list, "sv")).toBe("short");
    expect(names()).toEqual(["Nu", "Flöde", "Graf", "Intäkt", "Pris"]);
    expect(tabs.map((tab) => tab.getAttribute("aria-label"))).toEqual(long);
    expect(tabs.map((tab) => tab.title)).toEqual(long);
    // Each tab is held as wide as its selected form (the styles read the words), so choosing a tab never rewords.
    expect(tabs.map((tab) => tab.dataset["text"])).toEqual(["Nu", "Flöde", "Graf", "Intäkt", "Pris"]);
    room(200);
    expect(fitTabs(list, "sv")).toBe("icons");
    expect(names()).toEqual(["", "", "", "", ""]);
    expect(tabs.every((tab) => tab.querySelector("svg[aria-hidden='true']") !== null)).toBe(true);
    expect(tabs.map((tab) => tab.getAttribute("aria-label"))).toEqual(long);
    expect(tabs.map((tab) => tab.title)).toEqual(long);
    // Wider again: the long names come back, and the tooltips go.
    room(600);
    list.dataset["fit"] = "";
    expect(fitTabs(list, "sv")).toBe("long");
    expect(names()).toEqual(long);
    expect(tabs.every((tab) => !tab.hasAttribute("title"))).toBe(true);
    // A row not laid out (0 px) keeps its long names; a redraw starts from the last wording.
    expect(fitTabs(tabList(document, "sv", "now", "t", vi.fn()), "sv")).toBe("long");
    expect(tabList(document, "de", "now", "t", vi.fn(), "short").querySelector("[data-tab='graph']")?.textContent).toBe("Grafik");
    setTabFit(list, "en", "short");
    expect(names()).toEqual(["Now", "Flow", "Chart", "Earned", "Price"]);
  });

  it("moves with the arrow keys (wrapping), Home and End, and shows the tab moved to", () => {
    expect(tabForKey("ArrowRight", "now")).toBe("flow");
    expect(tabForKey("ArrowRight", "price")).toBe("now");
    expect(tabForKey("ArrowLeft", "now")).toBe("price");
    expect(tabForKey("Home", "graph")).toBe("now");
    expect(tabForKey("End", "now")).toBe("price");
    expect(tabForKey("Enter", "now")).toBeNull();
    const onSelect = vi.fn();
    const list = tabList(document, "en", "now", "t", onSelect);
    const first = list.querySelector<HTMLButtonElement>("[data-tab='now']")!;
    const key = new KeyboardEvent("keydown", { key: "End", bubbles: true, cancelable: true });
    first.dispatchEvent(key);
    expect(onSelect).toHaveBeenCalledWith("price", true);
    expect(key.defaultPrevented).toBe(true);
    list.querySelector<HTMLButtonElement>("[data-tab='flow']")!.click();
    expect(onSelect).toHaveBeenLastCalledWith("flow", false);
    // A tap on the tab shown does nothing.
    first.click();
    expect(onSelect).toHaveBeenCalledTimes(2);
  });

  it("remembers the tab per card in this browser, guarded like the chart preferences", () => {
    const store = new Map<string, string>();
    const memory = () => ({ getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, value: string) => void store.set(key, value) });
    expect(readTab("site_a", memory)).toBeNull();
    writeTab("site_a", "price", memory);
    expect(store.get(tabKey("site_a"))).toBe("price");
    expect(readTab("site_a", memory)).toBe("price");
    expect(readTab("site_b", memory)).toBeNull();
    store.set(tabKey("site_a"), "nonsense");
    expect(readTab("site_a", memory)).toBeNull();
    const throwing = () => ({
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    });
    expect(readTab("site_a", throwing)).toBeNull();
    expect(() => writeTab("site_a", "now", throwing)).not.toThrow();
    expect(readTab("site_a", () => null)).toBeNull();
  });
});

describe("the locked tab", () => {
  it("takes `tab` from the YAML, refuses a tab it does not know, and reads `all` as none", () => {
    expect(parseHouseConfig({ type: "custom:spotnav-house-card", site: "s", tab: "graph" })).toEqual({ type: "custom:spotnav-house-card", site: "s", tab: "graph" });
    expect(parseHouseConfig({ type: "custom:spotnav-house-card", site: "s", tab: "all" })).toEqual({ type: "custom:spotnav-house-card", site: "s" });
    expect(() => parseHouseConfig({ type: "x", site: "s", tab: "history" })).toThrow(/tab must be one of now, flow, graph, earn, price/);
    expect(parseHouseConfig({ type: "custom:spotnav-house-card", site: "s", tab: "earn" }).tab).toBe("earn");
  });

  it("offers the choice in the card editor's form", () => {
    const form = houseConfigForm("sv");
    const tab = (form.schema as Array<{ name: string; selector: { select?: { options: Array<{ value: string; label: string }> } } }>).find((field) => field.name === "tab")!;
    expect(tab.selector.select!.options.map((option) => option.label)).toEqual(["(alla)", "Nu", "Energiflöde", "Energigraf", "Intäkt", "Pris och batteri"]);
    expect(form.computeLabel({ name: "tab" })).toBe("Visa bara flik");
  });

  it("hides the tab row and leaves the panel without a tab to name it", () => {
    const card = render(house(), { locked: "price", tab: "now" });
    expect(card.querySelector("[role='tablist']")).toBeNull();
    const panel = card.querySelector<HTMLElement>(".spotnav-house-panel")!;
    expect(panel.dataset["tab"]).toBe("price");
    expect(panel.getAttribute("role")).toBeNull();
  });
});

describe("the period picker", () => {
  const day: PeriodChoice = { period: "day", date: null };

  it("steps one period back and forward, never past today, and keeps the moment when the kind changes", () => {
    expect(stepChoice(day, TODAY, -1)).toEqual({ period: "day", date: "2026-11-12" });
    expect(stepChoice({ period: "day", date: "2026-11-12" }, TODAY, 1)).toEqual({ period: "day", date: null });
    expect(stepChoice({ period: "day", date: "2026-11-12" }, TODAY, 5)).toEqual({ period: "day", date: null });
    expect(stepChoice({ period: "month", date: null }, TODAY, -1)).toEqual({ period: "month", date: "2026-10-01" });
    expect(stepChoice({ period: "year", date: null }, TODAY, -1)).toEqual({ period: "year", date: "2025-01-01" });
    expect(stepChoice({ period: "month", date: "2026-01-01" }, TODAY, -1)).toEqual({ period: "month", date: "2025-12-01" });
    expect(kindChoice({ period: "day", date: "2026-10-05" }, "month", TODAY)).toEqual({ period: "month", date: "2026-10-01" });
    expect(kindChoice({ period: "day", date: "2026-11-05" }, "month", TODAY)).toEqual({ period: "month", date: null });
    expect(kindChoice(day, "year", TODAY)).toEqual({ period: "year", date: null });
  });

  it("offers nothing after today: the forward step off, the date input's max, no later month", () => {
    const onChange = vi.fn();
    const today = periodPicker(document, "sv", day, TODAY, onChange);
    expect(today.querySelector<HTMLButtonElement>("[data-step='forward']")!.disabled).toBe(true);
    expect(today.querySelector<HTMLInputElement>("input[data-pick='day']")!.max).toBe(TODAY);
    today.querySelector<HTMLButtonElement>("[data-step='back']")!.click();
    expect(onChange).toHaveBeenLastCalledWith({ period: "day", date: "2026-11-12" });
    const input = today.querySelector<HTMLInputElement>("input[data-pick='day']")!;
    input.value = "2026-11-20";
    input.dispatchEvent(new Event("change"));
    expect(onChange).toHaveBeenCalledTimes(1);
    input.value = "2026-11-02";
    input.dispatchEvent(new Event("change"));
    expect(onChange).toHaveBeenLastCalledWith({ period: "day", date: "2026-11-02" });
    const past = periodPicker(document, "sv", { period: "day", date: "2026-11-02" }, TODAY, onChange);
    expect(past.querySelector<HTMLButtonElement>("[data-step='forward']")!.disabled).toBe(false);
    const month = periodPicker(document, "sv", { period: "month", date: null }, TODAY, onChange);
    const months = Array.from(month.querySelectorAll<HTMLOptionElement>("select[data-pick='month'] option")).map((option) => option.value);
    expect(months[0]).toBe("2026-11-01");
    expect(months.every((value) => value <= TODAY)).toBe(true);
    const kinds = Array.from(month.querySelectorAll<HTMLButtonElement>("button[data-kind]"));
    expect(kinds.map((button) => `${button.textContent}:${button.getAttribute("aria-pressed")}`)).toEqual(["Dag:false", "Månad:true", "År:false"]);
    kinds[2]!.click();
    expect(onChange).toHaveBeenLastCalledWith({ period: "year", date: null });
  });

  it("names the period in the reader's language, today's with how far its figures reach", () => {
    expect(choiceLabel("sv", { period: "day", date: null }, TODAY)).toBe("I dag");
    expect(choiceLabel("sv", { period: "day", date: "2026-11-12" }, TODAY)).toBe("I går");
    expect(choiceLabel("sv", { period: "day", date: "2026-11-11" }, TODAY)).toMatch(/^ons 11 nov/);
    expect(choiceLabel("en", { period: "day", date: "2026-11-11" }, TODAY)).toBe("Wed 11 Nov");
    expect(choiceLabel("sv", { period: "month", date: null }, TODAY)).toBe("nov 2026");
    expect(choiceLabel("sv", { period: "year", date: "2025-01-01" }, TODAY)).toBe("2025");
    const picker = periodPicker(document, "sv", { period: "day", date: null }, TODAY, vi.fn(), "17:00");
    expect(picker.querySelector("[data-pick-label]")?.textContent).toBe("I dag · till 17:00");
    // How far today's figures reach is a part of its own, which the styles drop whole where the row has no room for
    // it (never cut to "…"); the name for a screen reader and the tooltip keep it.
    const label = picker.querySelector<HTMLElement>("[data-pick-label]")!;
    expect(Array.from(label.children).map((part) => part.textContent)).toEqual(["I dag", " · till 17:00"]);
    expect(label.querySelector(".spotnav-house-picker-reach")?.textContent).toBe(" · till 17:00");
    expect(label.title).toBe("I dag · till 17:00");
    expect(label.getAttribute("aria-label")).toMatch(/: I dag · till 17:00$/);
    expect(TAB_STYLES).toMatch(/\.spotnav-house-picker-label:has\(\.spotnav-house-picker-reach\) \{[^}]*flex-wrap: wrap;/);
    // A month's or a year's list lies over the label, see-through, for a tap and the keyboard.
    const month = periodPicker(document, "sv", { period: "month", date: null }, TODAY, vi.fn(), "17:00");
    expect(month.querySelector("[data-pick-label]")?.textContent).toBe("nov 2026");
    expect(month.querySelector("select[data-pick='month']")?.className).toContain("spotnav-house-picker-over");
  });

  it("stops ten years back", () => {
    const old = periodPicker(document, "en", { period: "year", date: "2016-01-01" }, TODAY, vi.fn());
    expect(old.querySelector<HTMLButtonElement>("[data-step='back']")!.disabled).toBe(true);
  });

  it("is shared by the three period tabs and not shown in Now", () => {
    const choice: PeriodChoice = { period: "month", date: "2026-10-01" };
    for (const tab of ["flow", "graph", "price"] as const) {
      expect(render(house(), { tab, choice }).querySelector("select[data-pick='month']")).not.toBeNull();
    }
    expect(render(house(), { tab: "now", choice }).querySelector("[data-step]")).toBeNull();
  });
});

describe("Now", () => {
  it("draws only the nodes the site has, the live kW on each flowing line and its way", () => {
    const answer = house();
    const svg = renderSchematic({ doc: document, language: "sv", answer, live: live(), width: 340, idPrefix: "t" });
    const roles = Array.from(svg.querySelectorAll<SVGGElement>(".spotnav-now-node")).map((node) => node.dataset["role"]);
    expect(roles.sort()).toEqual(["battery", "car", "grid", "heat", "house", "sun"]);
    const labels = Object.fromEntries(Array.from(svg.querySelectorAll<SVGGElement>(".spotnav-now-pill")).map((pill) => [pill.dataset["role"], pill.textContent]));
    expect(labels).toEqual({ sun: "1,2 kW", grid: "0,4 kW", battery: "1,5 kW", heat: "1,1 kW" });
    // The car draws nothing: its line is still, without dots.
    expect(svg.querySelector(".spotnav-now-dots[data-role='car']")).toBeNull();
    expect(svg.querySelector(".spotnav-now-dots[data-role='grid']")?.getAttribute("data-tone")).toBe("import");
    expect(svg.querySelector(".spotnav-now-house-kw")?.textContent).toBe("3,1 kW");
    // Each day figure in its corner.
    expect(svg.querySelector(".spotnav-now-words[data-role='sun']")?.textContent).toBe("Sol i dag7,5 kWh");
    // Narrow: what was sold under what was bought, in the export's colour; wide: beside it.
    expect(svg.querySelector(".spotnav-now-words[data-role='grid']")?.textContent).toBe("Köpt i dag76,9 kWhsålt 0,1 kWh");
    expect(svg.querySelector(".spotnav-now-words[data-role='grid'] .spotnav-now-extra")?.getAttribute("data-tone")).toBe("export");
    const wide = renderSchematic({ doc: document, language: "sv", answer, live: live(), width: 760, idPrefix: "t" });
    expect(wide.querySelector(".spotnav-now-words[data-role='grid'] .spotnav-now-beside")?.textContent).toBe(" · sålt 0,1");
    // Each figure in its part's colour: the sun, bought, the battery, the car.
    expect(svg.querySelector(".spotnav-now-value[data-k='value-grid']")?.getAttribute("data-tone")).toBe("import");
    expect(svg.querySelector(".spotnav-now-value[data-k='value-sun']")?.getAttribute("data-tone")).toBe("sun");
    // The car plugged in: its level, and today's energy small under it.
    expect(svg.querySelector(".spotnav-now-words[data-role='car']")?.textContent).toBe("Bil73 %25,5 kWh i dag");
    // A filled arrowhead on each flowing line, which the styles show only where the dots stand still.
    expect(svg.querySelectorAll(".spotnav-now-arrow")).toHaveLength(4);
    expect(NOW_STYLES).toMatch(/\.spotnav-now-arrow \{[^}]*display: none;/);
    expect(NOW_STYLES).toMatch(/prefers-reduced-motion: reduce\) \{\s*\.spotnav-now-arrow \{ display: inline; \}/);
    // The house's day figure says the car is not in it (a battery's own app counts the charger in).
    expect(svg.querySelector(".spotnav-now-words[data-role='house']")?.textContent).toBe("Hus i dag29,4 kWhutan bilen");
    // The heat sits well below the house, so its line straight down is long enough to show its flow.
    const spots = nodeSpots(340, schematicHeight(340));
    expect(spots.heat.y - spots.house.y).toBeGreaterThanOrEqual(90);
    expect(svg.querySelector("title")?.textContent).toContain("Köper 0,4 kW från nätet");
    // A house alone: one low row, the grid to the house, no other corner.
    const alone = renderSchematic({ doc: document, language: "en", answer: house("house_learning.json"), live: live("house_alone_now.json"), width: 340, idPrefix: "t" });
    expect(Array.from(alone.querySelectorAll<SVGGElement>(".spotnav-now-node")).map((node) => node.dataset["role"]).sort()).toEqual(["grid", "house"]);
    expect(alone.getAttribute("height")).toBe("90");
    expect(Array.from(alone.querySelectorAll<SVGGElement>(".spotnav-now-words")).map((words) => words.dataset["corner"])).toEqual(["top-right", "top-left"]);
  });

  it("dims an unplugged car and draws no line to it", () => {
    const unplugged = { ...live(), car_connected: false, car_soc_pct: null };
    const svg = renderSchematic({ doc: document, language: "sv", answer: house(), live: unplugged, width: 340, idPrefix: "t" });
    expect(svg.querySelector(".spotnav-now-node[data-role='car']")?.getAttribute("data-dim")).toBe("true");
    expect(svg.querySelector(".spotnav-now-wire[data-role='car']")).toBeNull();
    expect(svg.querySelector(".spotnav-now-words[data-role='car']")?.textContent).toBe("Bil25,5 kWhi dag");
  });

  it("cuts a long corner name with an ellipsis and keeps it whole in a tooltip", () => {
    expect(fitName("SOL I DAG", 100)).toBe("SOL I DAG");
    expect(fitName("Chauffage aujourd'hui", 80)).toBe("Chauffage…");
    const svg = renderSchematic({ doc: document, language: "fr", answer: house(), live: live(), width: 300, idPrefix: "t" });
    for (const name of Array.from(svg.querySelectorAll(".spotnav-now-name"))) {
      expect(name.firstChild?.textContent?.length ?? 0).toBeLessThanOrEqual(Math.floor(100 / 7.2));
    }
  });

  it("patches a live refresh in place, the dots kept running, and draws anew when a line turns", () => {
    const answer = house();
    const host = document.createElement("div");
    const drawn = renderSchematic({ doc: document, language: "sv", answer, live: live(), width: 340, idPrefix: "t" });
    host.append(drawn);
    const dots = drawn.querySelector(".spotnav-now-dots[data-role='grid']");
    const more = { ...live(), grid_kw: 2.4, house_kw: 5.1 };
    expect(patchSchematic(drawn, renderSchematic({ doc: document, language: "sv", answer, live: more, width: 340, idPrefix: "t" }))).toBe(true);
    expect(host.firstElementChild).toBe(drawn);
    expect(drawn.querySelector(".spotnav-now-dots[data-role='grid']")).toBe(dots);
    expect(drawn.querySelector(".spotnav-now-pill[data-role='grid']")?.textContent).toBe("2,4 kW");
    expect(drawn.querySelector(".spotnav-now-house-kw")?.textContent).toBe("5,1 kW");
    // The grid turns to an export: a new drawing.
    const turned = renderSchematic({ doc: document, language: "sv", answer, live: { ...live(), grid_kw: -1.2 }, width: 340, idPrefix: "t" });
    expect(patchSchematic(drawn, turned)).toBe(false);
    expect(host.firstElementChild).toBe(turned);
  });

  it("says each line's way: an export and a charging battery away from the house, in the export's colour", () => {
    const lines = schematicLines(live("villa_export.json"), house());
    const by = Object.fromEntries(lines.map((line) => [line.role, line]));
    expect(by["grid"]).toMatchObject({ way: "out", tone: "export", kw: 2.6 });
    expect(by["battery"]).toMatchObject({ way: "out", kw: 1.2 });
    expect(by["sun"]).toMatchObject({ way: "in" });
    // No car plugged in: no line to it.
    expect(by["car"]).toBeUndefined();
    expect(schematicLines(live("villa_car.json"), house()).find((line) => line.role === "car")).toMatchObject({ way: "out", kw: 7.4 });
    // Faster dots with more power, within the lap's bounds.
    expect(dotLap(0)).toBeCloseTo(3.3);
    expect(dotLap(12)).toBeCloseTo(0.8);
    expect(dotLap(30)).toBeCloseTo(0.8);
    expect(dotLap(6)).toBeLessThan(dotLap(1));
  });

  it("has the day's figures and the battery's own power without the live flows (an older backend)", () => {
    const answer = house();
    const lines = schematicLines(null, { ...answer, battery: { ...answer.battery!, now: { action: null, power_kw: -0.6 } } });
    expect(lines.find((line) => line.role === "battery")).toMatchObject({ way: "in", kw: 0.6 });
    expect(lines.find((line) => line.role === "grid")).toMatchObject({ kw: null, way: "still" });
    const card = render(answer, { tab: "now" }, { live: null }, "en");
    expect(card.querySelector("[data-line='battery-sub']")?.textContent).toContain("no live figures yet");
  });

  it("says what the battery does and what its plan does next, and who runs it when SpotNav does not", () => {
    const answer = house();
    const runs = planRuns(answer, NOW);
    expect(runs.length).toBeGreaterThan(1);
    const controlled = { ...answer, battery: { ...answer.battery!, control: "on" } };
    expect(planSentence("sv", controlled, NOW)).toBe("Batteriet täcker huset och lagrar solen till 18:00, sedan säljer det till nätet.");
    expect(planSentence("sv", answer, NOW)).toBe("Batteriet styr sig självt (Sigenergy AI) · SpotNav skulle täcka huset och lagra solen till 18:00, sedan sälja till nätet.");
    const free = { ...answer, battery: { ...answer.battery!, management: "free" } };
    expect(planSentence("en", free, NOW)).toMatch(/^Only testing so far · SpotNav would cover the house and store the sun until 18:00/);
    expect(planSentence("sv", { ...answer, battery: { ...answer.battery!, status: "waiting" } }, NOW)).toBe(translate("sv", "house.saving.waiting"));
    expect(planSentence("sv", house("house_learning.json"), NOW)).toBeNull();
    expect(render(controlled, { tab: "now" }, {}, "sv").querySelector("[data-line='plan']")?.textContent).toMatch(/^Batteriet täcker huset/);
  });
});

describe("the period tabs' content", () => {
  it("Energy flow: today's flow from the card's answer; tiles only where no diagram can be drawn, never one of 0 kWh", () => {
    const answer = house();
    const today = render(answer, { tab: "flow" }, {}, "sv");
    expect(today.querySelector("[data-part='energy-flow'] svg")).not.toBeNull();
    // The diagram carries the figures: no tiles under it.
    expect(today.querySelector("[data-tile]")).toBeNull();
    const tiles = Array.from(tilesBlock(document, "sv", answer.energy_flow!)!.querySelectorAll<HTMLElement>("[data-tile]")).map((tile) => `${tile.dataset["tile"]}:${tile.textContent}`);
    expect(tiles).toEqual(["sun:7,5 kWhSol", "house:16,7 kWhHus", "discharged:8,6 kWhUrladdat", "charged:38,0 kWhLaddat", "bought:76,9 kWhKöpt", "sold:0,1 kWhSålt", "heat:12,6 kWhVärme", "car:25,5 kWhBil"]);
    // A morning with nothing yet between two blocks to draw: no diagram, so the tiles carry what there is, and a
    // part with no energy has none.
    const flow = answer.energy_flow!;
    const early = {
      ...answer,
      energy_flow: {
        ...flow,
        nodes: flow.nodes.map((node) => ({ ...node, kwh: node.role === "house" ? 0.3 : node.role === "grid" && node.side === "source" ? 0.3 : node.role === "heat" ? 0.04 : 0 })),
        links: [],
      },
    };
    const waiting = render(early, { tab: "flow" }, {}, "sv");
    expect(waiting.querySelector("[data-part='energy-flow'] svg")).toBeNull();
    expect(Array.from(waiting.querySelectorAll<HTMLElement>("[data-tile]")).map((tile) => `${tile.dataset["tile"]}:${tile.textContent}`)).toEqual(["house:0,3 kWhHus", "bought:0,3 kWhKöpt"]);
    const nothing = { ...early, energy_flow: { ...early.energy_flow, nodes: early.energy_flow.nodes.map((node) => ({ ...node, kwh: 0 })) } };
    expect(render(nothing, { tab: "flow" }, {}, "sv").querySelector("[data-part='tiles']")).toBeNull();
    expect(tilesBlock(document, "sv", nothing.energy_flow)).toBeNull();
    const month = history("villa_month.json");
    const loading = render(answer, { tab: "flow", choice: { period: "month", date: null } }, { history: { kind: "loading" } });
    expect(loading.querySelector("[data-line='loading']")?.getAttribute("aria-busy")).toBe("true");
    const shown = render(answer, { tab: "flow", choice: { period: "month", date: null } }, { history: { kind: "ready", answer: month } });
    expect(shown.querySelector("[data-part='energy-flow'] svg")).not.toBeNull();
    expect(shown.querySelector("[data-tile]")).toBeNull();
    const failed = render(answer, { tab: "flow", choice: { period: "month", date: null } }, { history: { kind: "failed", code: "unknown_command" } });
    expect(failed.querySelector("[data-line='failed']")?.textContent).toBe(translate("en", "hist.unsupported"));
    // A house alone has no sun, battery or heat tile, and none for what it neither sold nor charged.
    const alone = history("house_alone_year.json");
    expect(flowTiles(alone.energy_flow!).map((tile) => tile.key)).toEqual(["house", "bought"]);
  });

  it("Energy chart: today's hours with the forecast, a month by the day and a year by the month", () => {
    const answer = house();
    expect(render(answer, { tab: "graph" }).querySelector(".spotnav-house-surface svg")).not.toBeNull();
    const month = history("villa_month.json");
    const card = render(answer, { tab: "graph", choice: { period: "month", date: null } }, { history: { kind: "ready", answer: month } });
    const chart = card.querySelector<SVGSVGElement>(".spotnav-house-period-chart")!;
    // A full-width stack a day (the house and the heat here), the sun a stepped line over them, a kWh axis.
    const parts = Array.from(chart.querySelectorAll<SVGRectElement>(".spotnav-house-period-part"));
    expect(new Set(parts.map((part) => part.dataset["series"]))).toEqual(new Set(["house", "heat"]));
    expect(chart.querySelector(".spotnav-house-period-sun")?.getAttribute("d")).toMatch(/^M/);
    expect(Array.from(chart.querySelectorAll("text")).some((text) => text.textContent?.endsWith("kWh"))).toBe(true);
    expect(Array.from(card.querySelectorAll(".spotnav-house-pbars-key li")).map((item) => item.textContent)).toEqual(["House", "Heat", "Sun"]);
    // No battery series, no car without car energy.
    expect(card.textContent).not.toContain("Battery");
    // The keys walk the days, the readout says each.
    document.body.append(card);
    const surface = card.querySelector<HTMLElement>(".spotnav-house-period-surface")!;
    expect(card.querySelector(".spotnav-house-pbars-readout")?.textContent).toBe("Tap a bar for its figures.");
    surface.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }));
    expect(card.querySelector(".spotnav-house-pbars-readout")?.textContent).toMatch(/ · House \d/);
    card.remove();
    const year = history("villa_year.json");
    const yearChart = energyChart(document, "en", year, 340, 160, null)!;
    expect(yearChart.querySelectorAll(".spotnav-house-period-chart text").length).toBeGreaterThan(12);
    expect(niceStep(27, 3)).toBe(10);
    expect(niceStep(2.2, 3)).toBe(1);
  });

  it("Price and battery: what the battery earned under one heading, as kr bars on the price chart's hours", () => {
    const answer = house();
    const day = history("villa_day.json");
    const total = earnedTotal(day)!;
    expect(total).toBeCloseTo(day.buckets.reduce((sum, bucket) => sum + (bucket.arbitrage ?? 0), 0));
    // The day's history on the house fixture's day, so its hours meet the chart's.
    const today = { ...day, date: "2026-11-13", buckets: day.buckets.map((bucket) => ({ ...bucket, key: `2026-11-13${bucket.key.slice(10)}` })) };
    const card = render(answer, { tab: "price" }, { history: { kind: "ready", answer: today } }, "sv");
    expect(card.querySelector(".spotnav-house-surface .spotnav-house-price-buy")).not.toBeNull();
    expect(card.querySelector("[data-line='earned']")?.textContent).toBe("Batteriet tjänade 11 kr");
    expect(card.querySelector("[data-line='earned-sub']")?.textContent).toBe("Samma mått som Sigenergy-appen. · Elkostnad 38 kr");
    // The kr bars share the chart's x-axis: as wide, the same plot edges, under it, and a tap on them is that hour.
    const strip = card.querySelector<SVGSVGElement>(".spotnav-house-earned-strip")!;
    const chart = card.querySelector<SVGSVGElement>(".spotnav-house-surface svg")!;
    expect(strip.getAttribute("viewBox")!.split(" ")[2]).toBe(chart.getAttribute("viewBox")!.split(" ")[2]);
    expect(strip.querySelector(".spotnav-house-period-part[data-series='cost']")).not.toBeNull();
    expect(card.querySelector(".spotnav-house-earned-box")?.previousElementSibling?.classList.contains("spotnav-house-surface")).toBe(true);
    // One hint only.
    expect(card.textContent).not.toContain("Tryck på en stapel");
    // A house without a battery: no kr bars.
    const alone = history("house_alone_year.json");
    expect(earnedBars(document, "en", alone, 120, (minor) => `${minor}`, null)).toBeNull();
    // A year: a bar a month, its heading their sum.
    const year = render(answer, { tab: "price", choice: { period: "year", date: null } }, { history: { kind: "ready", answer: history("villa_year.json") } }, "sv");
    expect(year.querySelectorAll(".spotnav-house-pbar")).toHaveLength(12);
    expect(year.querySelector("[data-line='earned']")?.textContent).toMatch(/^Batteriet tjänade /);
    expect(year.querySelector("[data-line='earned-sub']")?.textContent).toMatch(/Elkostnad /);
    expect(year.querySelector("[data-line='saving']")).toBeNull();
  });

  it("reads a history bucket from a backend before the kr bars and the prices as none", () => {
    const raw = read("house_history", "villa_day.json");
    const buckets = (raw["buckets"] as Array<Record<string, unknown>>).map(({ arbitrage: _value, buy: _buy, sell: _sell, ...rest }) => rest);
    const decoded = decodeHouseHistory({ ...raw, buckets });
    expect(decoded.ok).toBe(true);
    const older = decoded.ok ? decoded.value : null!;
    expect(older.buckets.every((bucket) => bucket.arbitrage === null && bucket.buy === null && bucket.sell === null)).toBe(true);
    expect(earnedTotal(older)).toBeNull();
    expect(decodeHouseHistory({ ...raw, buckets: (raw["buckets"] as object[]).map((bucket) => ({ ...bucket, arbitrage: "1" })) }).ok).toBe(false);
  });
});

describe("the live flows' answer", () => {
  it("decodes the backend's own fixtures and refuses a mistyped or another version", () => {
    expect(live().house_kw).toBe(3.1);
    expect(live("house_alone_now.json").has).toEqual({ sun: false, battery: false, car: false, heat: false });
    const raw = read("house_now", "villa_now.json");
    expect(decodeHouseNow({ ...raw, api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    expect(decodeHouseNow({ ...raw, grid_kw: "1" })).toEqual({ ok: false, failure: "malformed" });
    const { has: _has, ...missing } = raw;
    expect(decodeHouseNow(missing)).toEqual({ ok: false, failure: "malformed" });
    expect(decodeHouseNow({ ...raw, later: 1 }).ok).toBe(true);
  });
});

describe("the card element's tabs", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
    localStorage.clear();
    if (customElements.get(HOUSE_CARD_ELEMENT) === undefined) {
      customElements.define(HOUSE_CARD_ELEMENT, SpotnavHouseCard);
    }
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  async function mount(config: Record<string, unknown>, hass: FakeHass): Promise<SpotnavHouseCard> {
    const element = document.createElement(HOUSE_CARD_ELEMENT) as SpotnavHouseCard;
    element.setConfig({ type: "custom:spotnav-house-card", site: "site_villa", ...config });
    element.hass = hass.snapshot("s", "en");
    document.body.append(element);
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(read("house", "villa_battery_sun.json"));
    await vi.advanceTimersByTimeAsync(0);
    return element;
  }

  it("polls the live flows every 30 s while Now is shown, and stops on another tab", async () => {
    const hass = new FakeHass();
    hass.liveHandler = async () => read("house_now", "villa_now.json");
    const element = await mount({}, hass);
    const root = element.shadowRoot!;
    expect(hass.liveMessages).toHaveLength(1);
    expect(root.querySelector(".spotnav-now-house-kw")?.textContent).toBe("3.1 kW");
    await vi.advanceTimersByTimeAsync(LIVE_REFRESH_MS);
    expect(hass.liveMessages).toHaveLength(2);
    root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='graph']")!.click();
    await vi.advanceTimersByTimeAsync(LIVE_REFRESH_MS * 3);
    expect(hass.liveMessages).toHaveLength(2);
    element.remove();
  });

  it("stops asking a backend without the live flows", async () => {
    const hass = new FakeHass();
    hass.liveHandler = async () => {
      throw Object.assign(new Error("unknown"), { code: "unknown_command" });
    };
    const element = await mount({}, hass);
    await vi.advanceTimersByTimeAsync(LIVE_REFRESH_MS * 3);
    expect(hass.liveMessages).toHaveLength(1);
    expect(element.shadowRoot!.querySelector("[data-line='battery-sub']")?.textContent).toContain("no live figures yet");
    element.remove();
  });

  it("remembers the tab chosen per card, focuses a tab moved to by key, and reads the period it needs", async () => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, value: string) => void store.set(key, value) });
    const hass = new FakeHass();
    const element = await mount({}, hass);
    const root = element.shadowRoot!;
    root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='now']")!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(root.activeElement?.getAttribute("data-tab")).toBe("flow");
    expect(store.get("spotnav-house-card.tab.site_villa")).toBe("flow");
    // Today's flow is the card's own answer: nothing more is read.
    expect(hass.messages).toHaveLength(1);
    root.querySelector<HTMLButtonElement>("button[data-kind='month']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages[1]).toEqual({ type: "spotnav/get_house_history", api_version: 1, site_id: "site_villa", period: "month" });
    hass.resolveNext(read("house_history", "villa_month.json"));
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[data-part='energy-flow'] svg")).not.toBeNull();
    // The period stays chosen across the tabs, read once.
    root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='graph']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages).toHaveLength(2);
    expect(root.querySelector(".spotnav-house-period-chart")).not.toBeNull();
    // A past day's hours in Energy chart: the day's own answer by its date.
    root.querySelector<HTMLButtonElement>("button[data-kind='day']")!.click();
    root.querySelector<HTMLButtonElement>("[data-step='back']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages.at(-1)).toEqual({ type: "spotnav/get_house", api_version: 1, site_id: "site_villa", date: "2026-11-12" });
    element.remove();
    // A new card for the same site opens on the tab left.
    const again = await mount({}, new FakeHass());
    expect(again.shadowRoot!.querySelector("[role='tab'][aria-selected='true']")?.getAttribute("data-tab")).toBe("graph");
    again.remove();
  });

  it("keeps the deep analysis in the details dialog, every section shown: the last days as the front once had them", async () => {
    const hass = new FakeHass();
    const element = await mount({}, hass);
    const root = element.shadowRoot!;
    root.querySelector<HTMLButtonElement>("button[data-history='open']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(read("house_history", "villa_month.json"));
    await vi.advanceTimersByTimeAsync(0);
    const dialog = root.querySelector<HTMLElement>("[role='dialog']")!;
    expect(dialog.querySelector("[data-detailed]")).toBeNull();
    const recent = dialog.querySelector<HTMLElement>("[data-part='recent']")!;
    expect(recent.querySelector("h4")?.textContent).toBe(translate("en", "hist.house.recent"));
    expect(recent.querySelector("[data-part='modes'][data-view='perfect']")).not.toBeNull();
    expect(recent.querySelector("[data-line='review']")).not.toBeNull();
    expect(recent.querySelectorAll(".spotnav-house-facts li").length).toBeGreaterThan(3);
    element.remove();
  });

  it("shows only the locked tab, without the row, and neither reads nor writes the remembered one", async () => {
    const store = new Map<string, string>([["spotnav-house-card.tab.site_villa", "now"]]);
    const setItem = vi.fn((key: string, value: string) => void store.set(key, value));
    vi.stubGlobal("localStorage", { getItem: (key: string) => store.get(key) ?? null, setItem });
    const hass = new FakeHass();
    const element = await mount({ tab: "flow" }, hass);
    const root = element.shadowRoot!;
    expect(root.querySelector("[role='tablist']")).toBeNull();
    expect(root.querySelector(".spotnav-house-panel")?.getAttribute("data-tab")).toBe("flow");
    expect(root.querySelector("[data-part='energy-flow'] svg")).not.toBeNull();
    expect(hass.liveMessages).toHaveLength(0);
    expect(setItem).not.toHaveBeenCalled();
    element.remove();
  });
});
