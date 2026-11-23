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

/** The same period without the sun's value (a site without the sun's production): the parts SpotNav controls alone. */
function sunless(answer: HouseHistory): HouseHistory {
  return { ...answer, review: answer.review === null ? null : { ...answer.review, sun_gave: null, sun_days: 0 }, buckets: answer.buckets.map((bucket) => ({ ...bucket, sun_gave: null })) };
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
    expect(earnParts(month)).toEqual(["own", "heat", "car", "sun", "would"]);
    expect(partTotal(month, "own")).toBe(2625);
    expect(partTotal(month, "would")).toBe(3225);
    expect(partTotal(month, "car")).toBe(1960);
    expect(partTotal(month, "heat")).toBe(120);
    expect(partTotal(month, "sun")).toBe(9100);
    expect(partTotal(month, "battery")).toBeNull();
    // The headline never counts what the plan would have earned: nothing was done. The sun's value is money the
    // house got: in it unless hidden.
    expect(earnTotal(month, new Set())).toBe(2625 + 120 + 1960 + 9100);
    expect(earnTotal(month, new Set(["own"]))).toBe(2080 + 9100);
    expect(earnTotal(month, new Set(["own", "heat", "car"]))).toBe(9100);
    expect(earnTotal(month, new Set(["own", "heat", "car", "sun"]))).toBeNull();
    const own = controlled(month);
    expect(earnParts(own)).toEqual(["battery", "heat", "car", "sun"]);
    expect(earnTotal(own, new Set())).toBe(4705 + 9100);
    expect(earnParts(sunless(own))).toEqual(["battery", "heat", "car"]);
    expect(earnTotal(sunless(own), new Set())).toBe(4705);
    // An answer from a backend before the parts: nothing to show.
    const old = history("villa_month.json", (raw) => {
      delete raw["own_control"];
      delete raw["saving"];
      for (const bucket of raw["buckets"] as Array<Record<string, unknown>>) {
        for (const key of ["own_battery", "would_battery", "saving", "saving_battery", "saving_car", "saving_heat", "sun_gave"]) {
          delete bucket[key];
        }
      }
    });
    expect(old.own_control).toBeNull();
    expect(old.buckets.every((bucket) => bucket.own_battery === null && bucket.would_battery === null && bucket.sun_gave === null)).toBe(true);
    // Its review still has the period's sun value (the same as Money's): the sun's sum, no bars.
    expect(earnParts(old)).toEqual(["sun"]);
    expect(earnBars({ doc: document, language: "sv", answer: old, hidden: new Set(), height: 150, money: kr, selected: null })).toBeNull();
    expect(earnParts({ ...old, review: null })).toEqual([]);
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
    const month = sunless(history("villa_month.json"));
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
    const month = sunless(history("villa_month.json"));
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
    const day = sunless(
      history("villa_day.json", (raw) => {
        const saving = raw["saving"] as Record<string, number>;
        saving["heat"] = 60;
        saving["heat_days"] = 1;
        saving["total"] = 360;
      }),
    );
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

describe("the sun's part", () => {
  it("is a toggle with its sum and what it counts, a part of the month's stacks and of the headline unless hidden", () => {
    const month = history("villa_month.json");
    // The same figure as Details → Money's (the period's reviews summed), its days in the bars.
    expect(partTotal(month, "sun")).toBe(month.review!.sun_gave);
    expect(month.buckets.reduce((sum, bucket) => sum + (bucket.sun_gave ?? 0), 0)).toBe(month.review!.sun_gave);
    const card = render(month);
    const toggle = card.querySelector<HTMLButtonElement>("button[data-legend='earn-sun']")!;
    expect(toggle.textContent).toBe("Sol 91 kr");
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
    expect(toggle.title).toBe("Solenergin huset använde, till priset då, plus det som såldes");
    expect(toggle.querySelector("[data-series='earn-sun']")).not.toBeNull();
    expect(card.querySelector<HTMLButtonElement>("button[data-legend='earn-car']")?.title).toBe("");
    expect(card.querySelector("[data-line='earn-total']")?.textContent).toBe("Intäkt 138 kr");
    const day = card.querySelector("[data-bar='2026-11-12']")!;
    expect(lanes(day)[0]!.series).toEqual(["earn-own", "earn-heat", "earn-car", "earn-sun"]);
    expect(day.getAttribute("aria-label")).toContain("Sol 7,00 kr");
    expect(card.querySelector("[data-line='earn-notes']")?.textContent).toContain(translate("sv", "house.earn.note.sun"));
    const onEarnPart = vi.fn();
    render(month, {}, { onEarnPart }).querySelector<HTMLButtonElement>("button[data-legend='earn-sun']")!.click();
    expect(onEarnPart).toHaveBeenCalledWith(["sun"]);
    // Hidden: out of the headline, the stacks and the notes, its toggle unpressed; remembered like the other parts.
    const hidden = render(month, { earnHidden: ["sun"] });
    expect(hidden.querySelector("[data-line='earn-total']")?.textContent).toBe("Intäkt 47 kr");
    expect(hidden.querySelector("button[data-legend='earn-sun']")?.getAttribute("aria-pressed")).toBe("false");
    expect(hidden.querySelector(".spotnav-house-pbar-part[data-series='earn-sun']")).toBeNull();
    expect(hidden.querySelector("[data-line='earn-notes']")?.textContent).not.toContain("Solens");
    const store = new Map<string, string>();
    const memory = () => ({ getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, value: string) => void store.set(key, value) });
    writeEarnHidden("site_a", ["sun", "car"], memory);
    expect(readEarnHidden("site_a", memory)).toEqual(["sun", "car"]);
    // A site without the sun's value has no such part.
    expect(render(sunless(month)).querySelector("button[data-legend='earn-sun']")).toBeNull();
    // A year: a part per month.
    const year = history("villa_year.json");
    expect(earnParts(year)).toContain("sun");
    expect(bucketPart(year.buckets.find((bucket) => bucket.key === "2026-11")!, "sun")).toBe(9100);
  });

  it("is a whole day's figure in a day's view: in the headline and its toggle, in no hour's bar", () => {
    const day = history("villa_day.json");
    expect(partTotal(day, "sun")).toBe(700);
    expect(day.buckets.every((bucket) => bucket.sun_gave === null)).toBe(true);
    const card = render(day, { choice: { period: "day", date: "2026-11-15" } });
    expect(Array.from(card.querySelectorAll<HTMLElement>("button[data-legend]")).map((toggle) => toggle.dataset["legend"])).toEqual(["earn-own", "earn-car", "earn-sun"]);
    expect(card.querySelector("[data-line='earn-total']")?.textContent).toBe("Intäkt 14 kr");
    expect(render(day, { choice: { period: "day", date: "2026-11-15" }, earnHidden: ["sun"] }).querySelector("[data-line='earn-total']")?.textContent).toBe("Intäkt 6,75 kr");
    expect(card.querySelector(".spotnav-house-pbar-part[data-series='earn-sun']")).toBeNull();
    expect(card.querySelectorAll("[data-part='earn-bars'] .spotnav-house-pbar")).toHaveLength(24);
    expect(card.querySelector("[data-line='earn-notes']")?.textContent).toContain(translate("sv", "house.earn.note.sunDay"));
    // A day whose hours are no longer kept still says so, the sun being a whole day's figure.
    const old = { ...day, buckets: day.buckets.map((bucket) => ({ ...bucket, saving: null, saving_car: null, own_battery: null })) };
    expect(earnNotes("sv", old, new Set(), kr)[0]).toBe(translate("sv", "house.earn.note.noHours"));
    // Only the sun shown: the headline is the sun's, no bars, and no word of missing hours.
    const sunOnly = render(day, { choice: { period: "day", date: "2026-11-15" }, earnHidden: ["own", "car"] });
    expect(sunOnly.querySelector("[data-line='earn-total']")?.textContent).toBe("Intäkt 7,00 kr");
    expect(sunOnly.querySelector("[data-part='earn-bars']")).toBeNull();
    expect(earnNotes("sv", day, new Set(["own", "car"]), kr)).toEqual([translate("sv", "house.earn.note.sunDay")]);
  });

  it("has its own hourly bars in a day's view on a site without a battery", () => {
    const day = history("solar_alone_day.json");
    expect(day.review).toBeNull();
    expect(day.sun).toEqual({ gave: 900, days: 1, period_days: 1, estimated_days: 0 });
    expect(partTotal(day, "sun")).toBe(900);
    const card = render(day, { choice: { period: "day", date: "2026-11-15" } });
    expect(Array.from(card.querySelectorAll<HTMLElement>("button[data-legend]")).map((toggle) => toggle.dataset["legend"])).toEqual(["earn-sun"]);
    expect(card.querySelector("[data-line='earn-total']")?.textContent).toBe("Intäkt 9,00 kr");
    // The sunny hours each have their bar; the night has none.
    const noon = card.querySelector("[data-bar='2026-11-15T12:00:00+01:00']")!;
    expect(lanes(noon)[0]!.series).toEqual(["earn-sun"]);
    expect(noon.getAttribute("aria-label")).toContain("Sol 2,25 kr");
    expect(card.querySelectorAll(".spotnav-house-pbar-part[data-series='earn-sun']")).toHaveLength(4);
    // The notes say what the sun counts, not that it is a whole day's figure; no estimate on a reviewed day.
    expect(earnNotes("sv", day, new Set(), kr)).toEqual([translate("sv", "house.earn.note.sun")]);
  });

  it("says where a site without a battery has days estimated from the hourly statistics", () => {
    const month = history("solar_alone_month.json");
    expect(month.sun).toEqual({ gave: 13512.5, days: 15, period_days: 15, estimated_days: 14 });
    expect(partTotal(month, "sun")).toBe(13512.5);
    expect(earnNotes("en", month, new Set(), kr)).toEqual([translate("en", "house.earn.note.sun"), translate("en", "house.sun.estimated")]);
    expect(bucketPart(month.buckets.find((bucket) => bucket.key === "2026-11-14")!, "sun")).toBe(912.5);
    // An older backend's answer has no sun block: decoded as none.
    const old = history("solar_alone_month.json", (raw) => {
      delete raw["sun"];
    });
    expect(old.sun).toBeNull();
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
    // The sun's part is remembered the same way.
    root.querySelector<HTMLButtonElement>("button[data-legend='earn-sun']")!.click();
    expect(store.get(earnKey("site_villa"))).toBe('["car","sun"]');
    expect(root.querySelector("button[data-legend='earn-sun']")?.getAttribute("aria-pressed")).toBe("false");
    element.remove();
    const again = await mount(new FakeHass());
    expect(store.get("spotnav-house-card.tab.site_villa")).toBe("earn");
    expect(again.shadowRoot!.querySelector("[role='tab'][aria-selected='true']")?.getAttribute("data-tab")).toBe("earn");
    again.remove();
  });
});
