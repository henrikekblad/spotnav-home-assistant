// The house card's tabs (SpotNav 3.0): the tablist and its keys, the tab remembered per card, the YAML's locked tab,
// the shared period picker and its limits, each tab's content, the live flows' decoder and older payloads.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HOUSE_CARD_ELEMENT, houseConfigForm, LIVE_REFRESH_MS, parseHouseConfig, SpotnavHouseCard } from "../src/house-card";
import { decodeHouseHistory, type HouseHistory } from "../src/house-history";
import { decodeHouse, type HouseAnswer } from "../src/house-model";
import { decodeHouseNow, dotLap, planRuns, planSentence, renderSchematic, schematicLines, type HouseNow } from "../src/house-now";
import { energyBars, flowTiles, valueBars, valueTotal } from "../src/house-period-view";
import { kindChoice, periodPicker, readTab, stepChoice, tabForKey, tabKey, tabList, writeTab, type PeriodChoice } from "../src/house-tabs";
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
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Nu", "Energiflöde", "Energigraf", "Pris och batteri"]);
    expect(tabs.map((tab) => tab.getAttribute("aria-selected"))).toEqual(["false", "false", "true", "false"]);
    expect(tabs.map((tab) => tab.tabIndex)).toEqual([-1, -1, 0, -1]);
    expect(tabs[2]!.getAttribute("aria-controls")).toBe("t-panel-graph");
    const card = render(house(), { tab: "graph" });
    const panel = card.querySelector("[role='tabpanel']")!;
    expect(panel.id).toBe("t-panel-graph");
    expect(panel.getAttribute("aria-labelledby")).toBe("t-tab-graph");
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
    expect(() => parseHouseConfig({ type: "x", site: "s", tab: "history" })).toThrow(/tab must be one of now, flow, graph, price/);
  });

  it("offers the choice in the card editor's form", () => {
    const form = houseConfigForm("sv");
    const tab = (form.schema as Array<{ name: string; selector: { select?: { options: Array<{ value: string; label: string }> } } }>).find((field) => field.name === "tab")!;
    expect(tab.selector.select!.options.map((option) => option.label)).toEqual(["(alla)", "Nu", "Energiflöde", "Energigraf", "Pris och batteri"]);
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
    const kind = month.querySelector<HTMLSelectElement>("select[data-pick-kind]")!;
    expect(Array.from(kind.options).map((option) => option.textContent)).toEqual(["Dag", "Månad", "År"]);
    kind.value = "year";
    kind.dispatchEvent(new Event("change"));
    expect(onChange).toHaveBeenLastCalledWith({ period: "year", date: null });
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
    expect(svg.querySelector(".spotnav-now-words[data-role='grid']")?.textContent).toBe("Nät i dag76,9 kWhsålt 0,1 kWh");
    expect(svg.querySelector("title")?.textContent).toContain("Köper 0,4 kW från nätet");
    // A house alone: the grid and the house only.
    const alone = renderSchematic({ doc: document, language: "en", answer: house("house_learning.json"), live: live("house_alone_now.json"), width: 340, idPrefix: "t" });
    expect(Array.from(alone.querySelectorAll<SVGGElement>(".spotnav-now-node")).map((node) => node.dataset["role"]).sort()).toEqual(["grid", "house"]);
  });

  it("says each line's way: an export and a charging battery away from the house, in the export's colour", () => {
    const lines = schematicLines(live("villa_export.json"), house());
    const by = Object.fromEntries(lines.map((line) => [line.role, line]));
    expect(by["grid"]).toMatchObject({ way: "out", tone: "export", kw: 2.6 });
    expect(by["battery"]).toMatchObject({ way: "out", kw: 1.2 });
    expect(by["sun"]).toMatchObject({ way: "in" });
    expect(by["car"]).toMatchObject({ way: "still" });
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
    expect(planSentence("sv", controlled, NOW)).toBe("Batteriet täcker huset och lagrar solen till 18:00, sedan säljer till nätet.");
    expect(planSentence("sv", answer, NOW)).toBe("Batteriet styr sig självt (Sigenergy AI) · SpotNav skulle täcka huset och lagra solen till 18:00, sedan sälja till nätet.");
    const free = { ...answer, battery: { ...answer.battery!, management: "free" } };
    expect(planSentence("en", free, NOW)).toMatch(/^Only testing so far · SpotNav would cover the house and store the sun until 18:00/);
    expect(planSentence("sv", { ...answer, battery: { ...answer.battery!, status: "waiting" } }, NOW)).toBe(translate("sv", "house.saving.waiting"));
    expect(planSentence("sv", house("house_learning.json"), NOW)).toBeNull();
    expect(render(controlled, { tab: "now" }, {}, "sv").querySelector("[data-line='plan']")?.textContent).toMatch(/^Batteriet täcker huset/);
  });
});

describe("the period tabs' content", () => {
  it("Energy flow: today's flow from the card's answer with a tile per block; another period from the history", () => {
    const answer = house();
    const today = render(answer, { tab: "flow" }, {}, "sv");
    expect(today.querySelector("[data-part='energy-flow'] svg")).not.toBeNull();
    const tiles = Array.from(today.querySelectorAll<HTMLElement>("[data-tile]")).map((tile) => `${tile.dataset["tile"]}:${tile.textContent}`);
    expect(tiles).toEqual(["sun:7,5 kWhSol", "house:16,7 kWhHus", "discharged:8,6 kWhUrladdat", "charged:38,0 kWhLaddat", "bought:76,9 kWhKöpt", "sold:0,1 kWhSålt", "heat:12,6 kWhVärme", "car:25,5 kWhBil"]);
    const month = history("villa_month.json");
    const loading = render(answer, { tab: "flow", choice: { period: "month", date: null } }, { history: { kind: "loading" } });
    expect(loading.querySelector("[data-line='loading']")?.getAttribute("aria-busy")).toBe("true");
    const shown = render(answer, { tab: "flow", choice: { period: "month", date: null } }, { history: { kind: "ready", answer: month } });
    expect(shown.querySelectorAll("[data-tile]")).toHaveLength(flowTiles(month.energy_flow!).length);
    const failed = render(answer, { tab: "flow", choice: { period: "month", date: null } }, { history: { kind: "failed", code: "unknown_command" } });
    expect(failed.querySelector("[data-line='failed']")?.textContent).toBe(translate("en", "hist.unsupported"));
    // A house alone has no sun, battery or heat tile.
    const alone = history("house_alone_year.json");
    expect(flowTiles(alone.energy_flow!).map((tile) => tile.key)).toEqual(["house", "bought", "sold", "car"]);
  });

  it("Energy chart: today's hours with the forecast, a month by the day and a year by the month", () => {
    const answer = house();
    expect(render(answer, { tab: "graph" }).querySelector(".spotnav-house-surface svg")).not.toBeNull();
    const month = history("villa_month.json");
    const card = render(answer, { tab: "graph", choice: { period: "month", date: null } }, { history: { kind: "ready", answer: month } });
    const bars = card.querySelectorAll<HTMLButtonElement>(".spotnav-house-pbar");
    expect(bars).toHaveLength(30);
    // The house's use stacked, the sun beside it.
    const full = card.querySelector<HTMLElement>(".spotnav-house-pbar[data-bar='2026-11-10']")!;
    expect(full.querySelectorAll(".spotnav-house-pbar-lane")).toHaveLength(2);
    expect(full.getAttribute("aria-label")).toMatch(/^.+ · House \d/);
    expect(Array.from(card.querySelectorAll(".spotnav-house-pbars-key li")).map((item) => item.textContent)).toEqual(["House", "Heat", "Sun"]);
    const year = history("house_alone_year.json");
    expect(energyBars(document, "en", year, 150, null)!.querySelectorAll(".spotnav-house-pbar")).toHaveLength(12);
    // Bars walk with the arrow keys from one tab stop.
    document.body.append(card);
    const first = bars[0]!;
    expect(Array.from(bars).filter((bar) => bar.tabIndex === 0)).toHaveLength(1);
    first.focus();
    first.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(card.querySelector(".spotnav-house-pbars-readout")?.textContent).toBe(bars[1]!.getAttribute("aria-label"));
    card.remove();
  });

  it("Price and battery: what the battery's energy was worth as kr bars, above and below zero, and its total", () => {
    const answer = house();
    const day = history("villa_day.json");
    const total = valueTotal(day)!;
    expect(total).toBeCloseTo(day.buckets.reduce((sum, bucket) => sum + (bucket.battery_value ?? 0), 0));
    const card = render(answer, { tab: "price" }, { history: { kind: "ready", answer: day } }, "sv");
    expect(card.querySelector(".spotnav-house-surface .spotnav-house-price-buy")).not.toBeNull();
    expect(card.querySelector("[data-line='value-total']")?.textContent).toMatch(/^Batteriets energi var värd /);
    const bars = card.querySelectorAll(".spotnav-house-pbar");
    expect(bars).toHaveLength(24);
    expect(card.querySelector(".spotnav-house-pbar-lane[data-below]")).not.toBeNull();
    expect(card.querySelector(".spotnav-house-pbars-zero")).not.toBeNull();
    // A house without a battery: no kr bars.
    const alone = history("house_alone_year.json");
    expect(valueBars(document, "en", alone, 120, (minor) => `${minor}`, null)).toBeNull();
    const month = render(answer, { tab: "price", choice: { period: "month", date: null } }, { history: { kind: "ready", answer: history("villa_month.json") } }, "sv");
    expect(month.querySelector("[data-line='period-cost']")?.textContent).toMatch(/^Elkostnad /);
    expect(month.querySelector("[data-line='today-hint']")).toBeNull();
  });

  it("reads a history bucket from a backend before the kr bars and the prices as none", () => {
    const raw = read("house_history", "villa_day.json");
    const buckets = (raw["buckets"] as Array<Record<string, unknown>>).map(({ battery_value: _value, buy: _buy, sell: _sell, ...rest }) => rest);
    const decoded = decodeHouseHistory({ ...raw, buckets });
    expect(decoded.ok).toBe(true);
    const older = decoded.ok ? decoded.value : null!;
    expect(older.buckets.every((bucket) => bucket.battery_value === null && bucket.buy === null && bucket.sell === null)).toBe(true);
    expect(valueTotal(older)).toBeNull();
    expect(decodeHouseHistory({ ...raw, buckets: (raw["buckets"] as object[]).map((bucket) => ({ ...bucket, battery_value: "1" })) }).ok).toBe(false);
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
    const kind = root.querySelector<HTMLSelectElement>("select[data-pick-kind]")!;
    kind.value = "month";
    kind.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages[1]).toEqual({ type: "spotnav/get_house_history", api_version: 1, site_id: "site_villa", period: "month" });
    hass.resolveNext(read("house_history", "villa_month.json"));
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelectorAll("[data-tile]").length).toBeGreaterThan(0);
    // The period stays chosen across the tabs, read once.
    root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='graph']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages).toHaveLength(2);
    expect(root.querySelectorAll(".spotnav-house-pbar")).toHaveLength(30);
    // A past day's hours in Energy chart: the day's own answer by its date.
    const day = root.querySelector<HTMLSelectElement>("select[data-pick-kind]")!;
    day.value = "day";
    day.dispatchEvent(new Event("change"));
    root.querySelector<HTMLButtonElement>("[data-step='back']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages.at(-1)).toEqual({ type: "spotnav/get_house", api_version: 1, site_id: "site_villa", date: "2026-11-12" });
    element.remove();
    // A new card for the same site opens on the tab left.
    const again = await mount({}, new FakeHass());
    expect(again.shadowRoot!.querySelector("[role='tab'][aria-selected='true']")?.getAttribute("data-tab")).toBe("graph");
    again.remove();
  });

  it("keeps the deep analysis in the history dialog: with Detailed on, the last days as the front once had them", async () => {
    const hass = new FakeHass();
    const element = await mount({}, hass);
    const root = element.shadowRoot!;
    root.querySelector<HTMLButtonElement>("button[data-history='open']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(read("house_history", "villa_month.json"));
    await vi.advanceTimersByTimeAsync(0);
    const dialog = root.querySelector<HTMLElement>("[role='dialog']")!;
    expect(dialog.querySelector("[data-part='recent']")).toBeNull();
    dialog.querySelector<HTMLButtonElement>("[data-detailed]")!.click();
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
