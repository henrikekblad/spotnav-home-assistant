// The house card's Earnings tab (SpotNav 3.0): the parts a period has, their sums and the headline, the bars per
// hour, day and month (a part that cost money below the line), the plan's would-be figure beside the battery's own
// before SpotNav controls it, the toggles remembered per card, the notes, and an older backend's answer.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HOUSE_CARD_ELEMENT, SpotnavHouseCard } from "../src/house-card";
import { bucketPart, earnBars, earnKey, earnLegend, earnNotes, earnParts, earnTotal, partTotal, readEarnHidden, writeEarnHidden } from "../src/house-earnings";
import { decodeHouseHistory, type HouseHistory } from "../src/house-history";
import { decodeHouse, type HouseAnswer } from "../src/house-model";
import { houseBody, type HouseTabsData, type HouseUi } from "../src/house-view";
import { translate } from "../src/i18n";
import { FakeHass } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const NOW = Date.parse("2026-11-13T16:20:00+00:00");

function read(...parts: string[]): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, unknown>;
}

function history(name: string, change: (raw: Record<string, unknown>) => void = () => undefined): HouseHistory {
  const raw = read("house_history", name);
  change(raw);
  const decoded = decodeHouseHistory(raw);
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

function house(): HouseAnswer {
  const decoded = decodeHouse(read("house", "villa_battery_sun.json"));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

/** The same period as if SpotNav had controlled the battery: its own part becomes SpotNav's. */
function controlled(answer: HouseHistory): HouseHistory {
  const own = answer.own_control!;
  return {
    ...answer,
    own_control: null,
    saving: { ...answer.saving!, battery: own.earned, battery_days: own.days, total: answer.saving!.total + own.earned },
    buckets: answer.buckets.map((bucket) => ({ ...bucket, saving_battery: bucket.own_battery, own_battery: null, would_battery: null })),
  };
}

const kr = (minor: number): string => `${(minor / 100).toFixed(2)} kr`;

function render(answer: HouseHistory, ui: Partial<HouseUi> = {}, handlers: Record<string, unknown> = {}, language: "en" | "sv" = "sv"): HTMLElement {
  const tabs: HouseTabsData = { live: null, history: { kind: "ready", answer }, day: null, today: "2026-11-16" };
  return houseBody({
    doc: document,
    language,
    state: { kind: "ready", answer: house() },
    ui: { selected: null, tab: "earn", choice: { period: answer.period, date: null }, ...ui },
    handlers: { onSelect: vi.fn(), ...handlers } as never,
    width: 340,
    nowMs: NOW,
    idPrefix: "t",
    tabs,
  });
}

function lanes(bar: Element): Array<{ series: string[]; below: boolean; left: string; width: string }> {
  return Array.from(bar.querySelectorAll<HTMLElement>(".spotnav-house-pbar-lane")).map((lane) => ({
    // A stack reads bottom up; its pieces are in the DOM from the top.
    series: Array.from(lane.querySelectorAll<HTMLElement>(".spotnav-house-pbar-part")).map((piece) => piece.dataset["series"]!).reverse(),
    below: lane.dataset["below"] === "true",
    left: lane.style.left,
    width: lane.style.width,
  }));
}

describe("the parts of a period", () => {
  it("has only the parts the period has, each with its sum, and a headline of the parts drawn", () => {
    const month = history("villa_month.json");
    // The battery under its own control: its own part and the plan's would-be figure, no arbitrage of SpotNav's.
    expect(earnParts(month)).toEqual(["own", "heat", "car", "would"]);
    expect(partTotal(month, "own")).toBe(2625);
    expect(partTotal(month, "would")).toBe(3225);
    expect(partTotal(month, "car")).toBe(1960);
    expect(partTotal(month, "heat")).toBe(120);
    expect(partTotal(month, "battery")).toBeNull();
    // The headline never counts what the plan would have earned: nothing was done.
    expect(earnTotal(month, new Set())).toBe(2625 + 120 + 1960);
    expect(earnTotal(month, new Set(["own"]))).toBe(2080);
    expect(earnTotal(month, new Set(["own", "heat", "car"]))).toBeNull();
    const own = controlled(month);
    expect(earnParts(own)).toEqual(["battery", "heat", "car"]);
    expect(earnTotal(own, new Set())).toBe(4705);
    // An answer from a backend before the parts: nothing to show.
    const old = history("villa_month.json", (raw) => {
      delete raw["own_control"];
      delete raw["saving"];
      for (const bucket of raw["buckets"] as Array<Record<string, unknown>>) {
        for (const key of ["own_battery", "would_battery", "saving", "saving_battery", "saving_car", "saving_heat"]) {
          delete bucket[key];
        }
      }
    });
    expect(old.own_control).toBeNull();
    expect(old.buckets.every((bucket) => bucket.own_battery === null && bucket.would_battery === null)).toBe(true);
    expect(earnParts(old)).toEqual([]);
    expect(decodeHouseHistory({ ...read("house_history", "villa_month.json"), own_control: { earned: "much" } })).toEqual({ ok: false, failure: "malformed" });
  });

  it("remembers the hidden parts per card in this browser, guarded like the chart preferences", () => {
    const store = new Map<string, string>();
    const memory = () => ({ getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, value: string) => void store.set(key, value) });
    expect(readEarnHidden("site_a", memory)).toEqual([]);
    writeEarnHidden("site_a", ["car", "nonsense"], memory);
    expect(store.get(earnKey("site_a"))).toBe('["car"]');
    expect(readEarnHidden("site_a", memory)).toEqual(["car"]);
    expect(readEarnHidden("site_b", memory)).toEqual([]);
    store.set(earnKey("site_a"), "{not json");
    expect(readEarnHidden("site_a", memory)).toEqual([]);
    store.set(earnKey("site_a"), '"car"');
    expect(readEarnHidden("site_a", memory)).toEqual([]);
    const throwing = () => ({
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    });
    expect(readEarnHidden("site_a", throwing)).toEqual([]);
    expect(() => writeEarnHidden("site_a", ["car"], throwing)).not.toThrow();
    expect(readEarnHidden("site_a", () => null)).toEqual([]);
  });
});

describe("the bars", () => {
  it("stacks a month's parts per day, with the plan's would-be figure hatched in a lane beside the battery's own", () => {
    const month = history("villa_month.json");
    const chart = earnBars({ doc: document, language: "sv", answer: month, hidden: new Set(), height: 150, money: kr, selected: null })!;
    const bars = Array.from(chart.querySelectorAll<HTMLButtonElement>(".spotnav-house-pbar"));
    expect(bars).toHaveLength(30);
    // The 12th: the battery's own, the heat and the car in one stack (bottom up), the plan's figure beside it.
    const day = bars.find((bar) => bar.dataset["bar"] === "2026-11-12")!;
    expect(lanes(day)).toEqual([
      { series: ["earn-own", "earn-heat", "earn-car"], below: false, left: "0%", width: "50%" },
      { series: ["earn-would"], below: false, left: "50%", width: "50%" },
    ]);
    expect(day.getAttribute("aria-label")).toBe("tors 12 nov.: urladdat 5,0 kWh, värt 15.00 kr, minus 11.25 kr som energin kostade att ladda = 3.75 kr · laddade 2,5 kWh för 2.00 kr (räknas när energin används) · Värme 0.60 kr · Bil 2.70 kr · SpotNav skulle ha sparat 4.75 kr");
    // Today (the 16th) is not reviewed yet: no would-be lane; a day before any figure says so.
    expect(lanes(bars.find((bar) => bar.dataset["bar"] === "2026-11-16")!).map((lane) => lane.series)).toEqual([["earn-own", "earn-car"]]);
    expect(bars[0]!.getAttribute("aria-label")).toBe("sön 1 nov.: ingen siffra");
    expect(bars[0]!.querySelector(".spotnav-house-pbar-lane")).toBeNull();
    // A part hidden leaves the stack; with the plan's figure hidden the stack takes the bar's whole width.
    const fewer = earnBars({ doc: document, language: "sv", answer: month, hidden: new Set(["would", "heat"]), height: 150, money: kr, selected: null })!;
    expect(lanes(fewer.querySelector("[data-bar='2026-11-12']")!)).toEqual([{ series: ["earn-own", "earn-car"], below: false, left: "0%", width: "100%" }]);
    // Under SpotNav's control: its arbitrage at the bottom of the stack, no lane beside it.
    const own = earnBars({ doc: document, language: "sv", answer: controlled(month), hidden: new Set(), height: 150, money: kr, selected: null })!;
    expect(lanes(own.querySelector("[data-bar='2026-11-12']")!)).toEqual([{ series: ["earn-battery", "earn-heat", "earn-car"], below: false, left: "0%", width: "100%" }]);
    // Nothing drawn: no chart.
    expect(earnBars({ doc: document, language: "sv", answer: month, hidden: new Set(["own", "heat", "car", "would"]), height: 150, money: kr, selected: null })).toBeNull();
  });

  it("puts a day's parts in their hours: the battery where it earned, the car where its charge started, a loss below the line", () => {
    const day = history("villa_day.json");
    expect(bucketPart(day.buckets[22]!, "car")).toBe(300);
    const chart = earnBars({ doc: document, language: "sv", answer: day, hidden: new Set(), height: 150, money: kr, selected: null })!;
    const bars = Array.from(chart.querySelectorAll<HTMLButtonElement>(".spotnav-house-pbar"));
    expect(bars).toHaveLength(24);
    expect(lanes(bars[22]!)).toEqual([{ series: ["earn-car"], below: false, left: "0%", width: "100%" }]);
    expect(bars[22]!.getAttribute("aria-label")).toBe("22:00: Bil 3.00 kr");
    expect(lanes(bars[17]!)).toEqual([{ series: ["earn-own"], below: false, left: "0%", width: "100%" }]);
    // 07:00: the battery gave back below what it was stored at.
    expect(lanes(bars[7]!)).toEqual([{ series: ["earn-own"], below: true, left: "0%", width: "100%" }]);
    expect(chart.querySelector(".spotnav-house-pbars-zero")).not.toBeNull();
    // One hour with a gain and a loss: a stack each side of the line, in the same place.
    const mixed = { ...day, buckets: day.buckets.map((bucket, index) => (index === 22 ? { ...bucket, own_battery: -40 } : bucket)) };
    const both = earnBars({ doc: document, language: "sv", answer: mixed, hidden: new Set(), height: 150, money: kr, selected: null })!;
    expect(lanes(both.querySelectorAll(".spotnav-house-pbar")[22]!)).toEqual([
      { series: ["earn-car"], below: false, left: "0%", width: "100%" },
      { series: ["earn-own"], below: true, left: "0%", width: "100%" },
    ]);
    // The arrow keys move between the hours, and the bar moved to says its figures.
    bars[21]!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }));
    expect(chart.querySelector(".spotnav-house-pbars-readout")?.textContent).toBe("22:00: Bil 3.00 kr");
    expect(bars[22]!.tabIndex).toBe(0);
  });
});

describe("the tab", () => {
  it("has the picker, the headline, the toggles with their sums, the bars and what each figure counts", () => {
    const month = history("villa_month.json");
    const onEarnPart = vi.fn();
    const card = render(month, {}, { onEarnPart });
    const panel = card.querySelector("[role='tabpanel']")!;
    expect(panel.getAttribute("data-tab")).toBe("earn");
    expect(panel.querySelector(".spotnav-house-picker")).not.toBeNull();
    expect(panel.querySelector("[data-line='earn-total']")?.textContent).toBe("Intäkt 47 kr");
    const toggles = Array.from(panel.querySelectorAll<HTMLButtonElement>("button[data-legend]"));
    expect(toggles.map((toggle) => `${toggle.dataset["legend"]}:${toggle.getAttribute("aria-pressed")}:${toggle.textContent}`)).toEqual([
      "earn-own:true:Batteri, egen styrning 26 kr",
      "earn-heat:true:Värme 1,20 kr",
      "earn-car:true:Bil 20 kr",
      "earn-would:true:SpotNav skulle ha sparat 32 kr",
    ]);
    expect(panel.querySelector("ul[aria-label='Delar som visas']")).not.toBeNull();
    expect(panel.querySelectorAll("[data-part='earn-bars'] .spotnav-house-pbar")).toHaveLength(30);
    expect(panel.querySelector("[data-line='earn-notes']")?.textContent).toBe(
      "Vad SpotNav skulle ha sparat är känt för 6 av 7 dagar; övriga räknas som de gick. Bilens besparing räknas på dagen då laddningen startade. Värmens besparing är en uppskattning.",
    );
    toggles[2]!.click();
    expect(onEarnPart).toHaveBeenCalledWith(["car"]);
    // With the car hidden: out of the headline and the bars, its toggle unpressed, its note gone.
    const hidden = render(month, { earnHidden: ["car"] }, { onEarnPart });
    expect(hidden.querySelector("[data-line='earn-total']")?.textContent).toBe("Intäkt 27 kr");
    expect(hidden.querySelector("button[data-legend='earn-car']")?.getAttribute("aria-pressed")).toBe("false");
    expect(hidden.querySelector("[data-series='earn-car'].spotnav-house-pbar-part")).toBeNull();
    expect(hidden.querySelector("[data-line='earn-notes']")?.textContent).not.toContain("Bilens");
    hidden.querySelector<HTMLButtonElement>("button[data-legend='earn-car']")!.click();
    expect(onEarnPart).toHaveBeenLastCalledWith([]);
    // Every part hidden: the headline says so, the toggles stay.
    const none = render(month, { earnHidden: ["own", "heat", "car", "would"] });
    expect(none.querySelector("[data-line='earn-total']")?.textContent).toBe("Ingen del vald.");
    expect(none.querySelector("[data-part='earn-bars']")).toBeNull();
    expect(none.querySelectorAll("button[data-legend]")).toHaveLength(4);
  });

  it("shows a day by the hour: the plan's figure in words, the car's hour and the heat's whole-day estimate said", () => {
    const day = history("villa_day.json", (raw) => {
      const saving = raw["saving"] as Record<string, number>;
      saving["heat"] = 60;
      saving["heat_days"] = 1;
      saving["total"] = 360;
    });
    const card = render(day, { choice: { period: "day", date: "2026-11-15" } });
    // The heat is in the headline and its toggle, in no hour's bar.
    expect(card.querySelector("[data-line='earn-total']")?.textContent).toBe("Intäkt 7,35 kr");
    expect(Array.from(card.querySelectorAll<HTMLElement>("button[data-legend]")).map((toggle) => toggle.dataset["legend"])).toEqual(["earn-own", "earn-heat", "earn-car"]);
    expect(card.querySelector(".spotnav-house-pbar-part[data-series='earn-heat']")).toBeNull();
    expect(card.querySelector("[data-line='earn-notes']")?.textContent).toBe(
      "På batteriet skulle SpotNav ha sparat 4,75 kr den här dagen. Bilens besparing ligger på timmen då laddningen startade. Värmens besparing är en uppskattning för hela dagen och ritas inte per timme.",
    );
    // The running day is reviewed tonight.
    const running = { ...day, today: day.date, own_control: { ...day.own_control!, would: null, would_days: 0 } };
    expect(earnNotes("sv", running, new Set(), kr)[0]).toBe("Vad SpotNav skulle ha sparat på batteriet räknas ut i natt.");
    // An older day: its hours are no longer kept, its sums are.
    const old = { ...day, buckets: day.buckets.map((bucket) => ({ ...bucket, saving: null, saving_car: null, own_battery: null })) };
    const kept = render(old, { choice: { period: "day", date: "2026-11-15" } });
    expect(kept.querySelector("[data-part='earn-bars']")).toBeNull();
    expect(kept.querySelector("[data-line='earn-total']")?.textContent).toBe("Intäkt 7,35 kr");
    expect(kept.querySelector("[data-line='earn-notes']")?.textContent).toMatch(/^Den här dagen saknar timvärden \(de sparas i 40 dagar\)\./);
  });

  it("says when there is nothing, is loading or failed, and drops its words in a box of fixed height", () => {
    const alone = history("house_alone_year.json");
    const empty = render({ ...alone, saving: null, buckets: alone.buckets.map((bucket) => ({ ...bucket, saving: null, saving_car: null, saving_heat: null })) }, { choice: { period: "year", date: null } });
    expect(empty.querySelector("[data-line='no-earnings']")?.textContent).toBe(translate("sv", "house.earn.none"));
    expect(empty.querySelector("button[data-legend]")).toBeNull();
    const boxed = render(history("villa_month.json"), { fill: 200 });
    expect(boxed.querySelector("[data-line='earn-notes']")).toBeNull();
    expect(boxed.querySelector<HTMLElement>(".spotnav-house-pbars-plot")?.style.height).toBe("200px");
    expect(boxed.querySelector(".spotnav-house-pbars-readout")?.textContent).toBe("");
    const legend = earnLegend(document, "en", history("villa_month.json"), new Set(), kr, vi.fn());
    expect(legend.getAttribute("aria-label")).toBe("Parts shown");
    expect(legend.querySelector("button")?.textContent).toBe("Battery, own control 26.25 kr");
  });
});

describe("the card element", () => {
  beforeEach(() => {
    if (customElements.get(HOUSE_CARD_ELEMENT) === undefined) {
      customElements.define(HOUSE_CARD_ELEMENT, SpotnavHouseCard);
    }
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    document.body.replaceChildren();
  });

  it("reads the period for the tab, also today's, and remembers the parts hidden per card", async () => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, value: string) => void store.set(key, value) });
    const mount = async (hass: FakeHass): Promise<SpotnavHouseCard> => {
      const element = document.createElement(HOUSE_CARD_ELEMENT) as SpotnavHouseCard;
      element.setConfig({ type: "custom:spotnav-house-card", site: "site_villa" });
      element.hass = hass.snapshot("s", "en");
      document.body.append(element);
      await vi.advanceTimersByTimeAsync(0);
      hass.resolveNext(read("house", "villa_battery_sun.json"));
      await vi.advanceTimersByTimeAsync(0);
      return element;
    };
    const hass = new FakeHass();
    const element = await mount(hass);
    const root = element.shadowRoot!;
    root.querySelector<HTMLButtonElement>("[role='tab'][data-tab='earn']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages.at(-1)).toEqual({ type: "spotnav/get_house_history", api_version: 1, site_id: "site_villa", period: "day" });
    expect(root.querySelector("[data-line='loading']")).not.toBeNull();
    hass.resolveNext(read("house_history", "villa_day.json"));
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelectorAll("[data-part='earn-bars'] .spotnav-house-pbar")).toHaveLength(24);
    const car = root.querySelector<HTMLButtonElement>("button[data-legend='earn-car']")!;
    car.focus();
    car.click();
    expect(store.get(earnKey("site_villa"))).toBe('["car"]');
    // The toggle is drawn anew unpressed, and keeps the focus.
    expect(root.querySelector("button[data-legend='earn-car']")?.getAttribute("aria-pressed")).toBe("false");
    expect(root.activeElement?.getAttribute("data-legend")).toBe("earn-car");
    element.remove();
    const again = await mount(new FakeHass());
    expect(store.get("spotnav-house-card.tab.site_villa")).toBe("earn");
    expect(again.shadowRoot!.querySelector("[role='tab'][aria-selected='true']")?.getAttribute("data-tab")).toBe("earn");
    again.remove();
  });
});
