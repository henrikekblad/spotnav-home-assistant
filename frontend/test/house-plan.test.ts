// The house card's Plan tab: the decoder on the backend's own fixtures (`tests/fixtures/plan/`), the summary, the
// stacked chart against its labelled ceiling, the lanes with their line of why, the plan in words, a load SpotNav only
// watches marked "would", the fuse as the ceiling where there is no power tariff, and the card reading
// `spotnav/get_plan` while the tab is shown.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HOUSE_CARD_ELEMENT, SpotnavHouseCard } from "../src/house-card";
import { decodeHouse } from "../src/house-model";
import { decodePlan, planBody, priceTones, resetPlanSections, runWords, type PlanAnswer } from "../src/house-plan";
import { HOUSE_TABS } from "../src/house-tabs";
import { houseBody } from "../src/house-view";
import { LANGUAGES, translate } from "../src/i18n";
import { FakeHass } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function read(...parts: string[]): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, unknown>;
}

function plan(name = "villa.json"): PlanAnswer {
  const decoded = decodePlan(read("plan", name));
  if (!decoded.ok) {
    throw new Error(`the fixture must decode: ${decoded.failure}`);
  }
  return decoded.value;
}

function body(value: PlanAnswer, language: (typeof LANGUAGES)[number] = "sv", width = 360): HTMLElement {
  return planBody({ doc: document, language, plan: value, width });
}

describe("decoding the plan", () => {
  it("reads both fixtures, every hour and load", () => {
    const villa = plan();
    expect(villa.hours).toHaveLength(28);
    expect(villa.loads.map((load) => load.kind)).toEqual(["car", "home_battery", "heat", "heat_zone", "water_heater"]);
    expect(villa.would).toBe(true);
    const flat = plan("car_only.json");
    expect(flat.loads.map((load) => load.kind)).toEqual(["car"]);
    expect(flat.month_peak).toBeNull();
  });

  it("refuses another version, a missing field and a load kind it does not know", () => {
    expect(decodePlan({ ...read("plan", "villa.json"), api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    const { hours: _hours, ...missing } = read("plan", "villa.json");
    expect(decodePlan(missing).ok).toBe(false);
    const raw = read("plan", "villa.json");
    const loads = (raw.loads as Record<string, unknown>[]).map((load, index) => (index === 0 ? { ...load, kind: "spa" } : load));
    expect(decodePlan({ ...raw, loads }).ok).toBe(false);
  });
});

describe("the Plan tab", () => {
  it("heads with the rolling window and sums it up: the cost, the highest hour against the ceiling, the month's peak", () => {
    const root = body(plan());
    expect(root.querySelector("h3")?.textContent).toBe("Plan för de närmaste 28 timmarna");
    const stats = Array.from(root.querySelectorAll("[data-part='summary'] .spotnav-plan-stat")).map((stat) => stat.textContent);
    expect(stats).toHaveLength(3);
    expect(stats[0]).toContain("Beräknad kostnad");
    expect(stats[1]).toContain("8,3 kW");
    expect(stats[1]).toContain("kl. 03:00 · över taket");
    expect(root.querySelector("[data-over='true']")).not.toBeNull();
    expect(stats[2]).toContain("Månadens topp hittills");
    expect(stats[2]).toContain("5,4 kW");
  });

  it("stacks the loads above zero and the sun and the battery's cover below, with the net line and the labelled ceiling", () => {
    const root = body(plan());
    const layers = new Set(Array.from(root.querySelectorAll("svg rect[data-layer]")).map((rect) => rect.getAttribute("data-layer")));
    expect([...layers].sort()).toEqual(["battery_in", "battery_out", "car", "heat", "house", "sun", "water_heater"]);
    expect(root.querySelector("svg .spotnav-plan-net")).not.toBeNull();
    expect(root.querySelector("svg .spotnav-plan-ceiling")).not.toBeNull();
    expect(root.querySelector("svg .spotnav-plan-ceiling-label")?.textContent).toBe("Effekttak 5,0 kW");
    expect(root.querySelectorAll("svg .spotnav-plan-over").length).toBeGreaterThan(0);
    // The price strip, one mark per priced hour.
    expect(root.querySelectorAll("svg rect[data-tone]")).toHaveLength(28);
    // Midnight is marked: the window rolls into tomorrow.
    expect(root.querySelectorAll("svg .spotnav-plan-midnight")).toHaveLength(1);
  });

  it("marks what SpotNav only watches as would, in the chart, the lanes and the words", () => {
    const root = body(plan());
    expect(root.querySelector("[data-note='would']")?.textContent).toContain("skulle göra");
    expect(root.querySelector("svg rect[data-layer='heat'][data-would='true']")).not.toBeNull();
    expect(root.querySelector("svg rect[data-layer='car'][data-would='false']")).not.toBeNull();
    const heatLane = root.querySelector("[data-load='heat'][data-would='true'] .spotnav-plan-reason");
    expect(heatLane?.textContent).toBe("Skulle: Sänkt 4 h, förvärmd 2 h");
    const words = Array.from(root.querySelectorAll(".spotnav-plan-words li[data-load='heat']")).map((item) => item.textContent);
    expect(words.every((line) => line?.endsWith("skulle"))).toBe(true);
    expect(root.querySelectorAll(".spotnav-plan-words li[data-load='car'] .spotnav-plan-would")).toHaveLength(0);
  });

  it("says the plan in words, in time order, with the battery's grid charging in its own words", () => {
    const root = body(plan());
    const lines = Array.from(root.querySelectorAll(".spotnav-plan-words li")).map((item) => item.textContent);
    expect(lines[0]).toBe("20–23Husbatteriet täcker huset");
    expect(lines).toContain("23–01Beredaren värms: billig timme");
    expect(lines).toContain("01–05Garage laddar 14,8 kWh med 3,7 kW");
    expect(lines).toContain("03–05Nätladdning av husbatteriet, 3,0 kW");
    expect(lines.join(" ")).not.toMatch(/nätladda\b/i);
    expect(root.querySelector("[data-load='home_battery'] .spotnav-plan-reason")?.textContent).toBe(
      "Täcker 16,2 kWh · nätladdning av husbatteriet 6,0 kWh · laddar 3,6 kWh av solen",
    );
  });

  it("has the fuse as the ceiling and no month's peak where the price has no power tariff", () => {
    const root = body(plan("car_only.json"));
    expect(root.querySelector("svg .spotnav-plan-ceiling-label")?.textContent).toBe("Huvudsäkring ≈ 13,8 kW");
    const stats = Array.from(root.querySelectorAll("[data-part='summary'] .spotnav-plan-stat")).map((stat) => stat.textContent);
    expect(stats[2]).toContain("Effekttaxa");
    expect(stats[2]).toContain("ingen i elpriset");
    expect(stats[1]).toContain("under taket");
    expect(root.querySelector("[data-note='would']")).toBeNull();
    expect(root.querySelectorAll(".spotnav-plan-lane[data-load]")).toHaveLength(1);
  });

  it("labels the ceiling in the reader's language", () => {
    expect(body(plan("car_only.json"), "en").querySelector("svg .spotnav-plan-ceiling-label")?.textContent).toBe("Main fuse ≈ 13.8 kW");
  });

  it("tones the prices by thirds of the window", () => {
    expect(priceTones(plan().hours).filter((tone) => tone === "dear").length).toBeGreaterThan(0);
    expect(priceTones(plan().hours.map((hour) => ({ ...hour, price: null })))).toEqual(new Array(28).fill(null));
  });

  it("is worded in every language, nothing left as a key", () => {
    for (const language of LANGUAGES) {
      const text = body(plan(), language).textContent ?? "";
      expect(text, language).not.toMatch(/house\.plan\./);
      const value = plan();
      for (const load of value.loads) {
        for (const run of load.runs) {
          expect(runWords(language, load, run), language).not.toMatch(/house\.plan\./);
        }
      }
      expect(translate(language, "house.tab.plan").length, language).toBeGreaterThan(0);
    }
  });

  it("words the shed order's steps: paused and lowered for the ceiling", () => {
    const heater = plan().loads.find((load) => load.kind === "water_heater")!;
    const heat = plan().loads.find((load) => load.kind === "heat")!;
    const run = { ...heater.runs[0]!, what: "paused", reason: "ceiling" };
    expect(runWords("sv", heater, run)).toBe("Beredaren pausad: för taket");
    expect(runWords("en", heat, { ...heat.runs[0]!, what: "lower", reason: "ceiling" })).toBe("Heating lowered: for the ceiling");
    for (const language of LANGUAGES) {
      expect(runWords(language, heater, run), language).not.toMatch(/house\.plan\./);
    }
  });

  it("fits a phone's width without a fixed wider box", () => {
    const root = body(plan(), "sv", 300);
    const chart = root.querySelector("svg")!;
    expect(Number(chart.getAttribute("width"))).toBeLessThanOrEqual(300);
  });
});

describe("the Plan tab's folding sections", () => {
  function memory(): { store: () => { getItem(key: string): string | null; setItem(key: string, value: string): void }; saved: Map<string, string> } {
    const saved = new Map<string, string>();
    return { saved, store: () => ({ getItem: (key: string) => saved.get(key) ?? null, setItem: (key: string, value: string) => void saved.set(key, value) }) };
  }
  function section(root: HTMLElement, name: string): { heading: HTMLElement; button: HTMLButtonElement | null; content: HTMLElement } {
    return {
      heading: root.querySelector<HTMLElement>(`h4[data-section='${name}']`)!,
      button: root.querySelector<HTMLButtonElement>(`h4[data-section='${name}'] button`),
      content: root.querySelector<HTMLElement>(`div[data-section='${name}']`)!,
    };
  }
  beforeEach(() => resetPlanSections());

  it("folds both sections by default, with a hint of what is inside, and remembers what the viewer opens", () => {
    const { store, saved } = memory();
    const root = planBody({ doc: document, language: "sv", plan: plan("car_only.json"), width: 360, store });
    const lanes = section(root, "lanes");
    const words = section(root, "words");
    expect(lanes.button?.getAttribute("aria-expanded")).toBe("false");
    expect(lanes.button?.type).toBe("button");
    expect(lanes.content.hidden).toBe(true);
    expect(lanes.button?.textContent).toBe("Per last · 1");
    expect(words.content.hidden).toBe(true);
    const rows = words.content.querySelectorAll("li").length;
    expect(words.button?.textContent).toBe(`I ord · ${rows} ${rows === 1 ? "rad" : "rader"}`);
    words.button!.click();
    expect(words.button!.getAttribute("aria-expanded")).toBe("true");
    expect(words.content.hidden).toBe(false);
    expect(saved.get(`spotnav-house-card.plan.words.${plan("car_only.json").site_id}`)).toBe("open");
    // A new drawing, as after the next update, keeps the choice; so does a new page reading it back.
    const again = planBody({ doc: document, language: "sv", plan: plan("car_only.json"), width: 360, store });
    expect(section(again, "words").content.hidden).toBe(false);
    expect(section(again, "lanes").content.hidden).toBe(true);
    resetPlanSections();
    const reloaded = planBody({ doc: document, language: "sv", plan: plan("car_only.json"), width: 360, store });
    expect(section(reloaded, "words").content.hidden).toBe(false);
  });

  it("holds the choice on the page where nothing can be stored", () => {
    const store = () => {
      throw new Error("blocked");
    };
    const root = planBody({ doc: document, language: "en", plan: plan("car_only.json"), width: 360, store });
    section(root, "lanes").button!.click();
    const again = planBody({ doc: document, language: "en", plan: plan("car_only.json"), width: 360, store });
    expect(section(again, "lanes").content.hidden).toBe(false);
  });

  it("never folds away a car's does-not-fit choice: Per load is open under a plain heading", () => {
    const { store } = memory();
    const root = planBody({ doc: document, language: "sv", plan: plan(), width: 360, store, onDoesNotFit: vi.fn() });
    const lanes = section(root, "lanes");
    expect(lanes.button).toBeNull();
    expect(lanes.heading.textContent).toBe("Per last");
    expect(lanes.content.hidden).toBe(false);
    expect(lanes.content.querySelector("[data-part='does-not-fit'] button")).not.toBeNull();
    expect(section(root, "words").content.hidden).toBe(true);
  });

  it("has the hint's words in every language", () => {
    for (const language of LANGUAGES) {
      expect(translate(language, "house.plan.rows.other", { count: "6" }), language).toContain("6");
    }
  });
});

describe("the Plan tab on the card", () => {
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
  });

  it("is a tab after Now", () => {
    expect(HOUSE_TABS.slice(0, 2)).toEqual(["now", "plan"]);
  });

  it("reads spotnav/get_plan once the tab is shown, and draws it", async () => {
    const hass = new FakeHass();
    const element = document.createElement(HOUSE_CARD_ELEMENT) as SpotnavHouseCard;
    element.setConfig({ type: "custom:spotnav-house-card", site: "site_villa", tab: "plan" });
    element.hass = hass.snapshot("s", "en");
    document.body.append(element);
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(read("house", "villa_battery_sun.json"));
    await vi.advanceTimersByTimeAsync(0);
    const root = element.shadowRoot!;
    expect(root.querySelector("[data-line='plan-loading']")).not.toBeNull();
    const asked = hass.messages.at(-1)!;
    expect(asked).toMatchObject({ type: "spotnav/get_plan", api_version: 1, site_id: "site_villa" });
    hass.resolveNext(read("plan", "villa.json"));
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector(".spotnav-plan h3")?.textContent).toBe("Plan for the next 28 hours");
    element.remove();
  });

  it("says the house loading line while the plan is on its way in the body", () => {
    const decoded = decodeHouse(read("house", "villa_battery_sun.json"));
    if (!decoded.ok) throw new Error("fixture");
    const card = houseBody({
      doc: document,
      language: "en",
      state: { kind: "ready", answer: decoded.value },
      ui: { selected: null, tab: "plan" },
      handlers: { onSelect: vi.fn() } as never,
      width: 340,
      nowMs: Date.now(),
      idPrefix: "plan-test",
      tabs: { live: null, history: null, day: null, today: "2026-11-13", plan: { kind: "failed", code: null } },
    });
    expect(card.querySelector("[data-line='plan-failed']")).not.toBeNull();
  });
});
