// The heat card's hot water (SpotNav 3.0): the Boost button on Now and its states, the planned boosts and the peaks
// on Plan's day chart, the strip's hot-water time, and the review with the gate's progress, on the backend's own
// fixtures (`tests/fixtures/heat_card/`, the hot water as `heat/dhw_runtime.py` answers it).

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeAll, describe, expect, it, vi } from "vitest";

import { decodeHeat, decodeHeatBoost, type HeatAnswer, type HeatBoost } from "../src/heat-model";
import { aheadLine, boostWords, heatBody, hotWaterLines, plannedWaterText, type HeatState } from "../src/heat-view";
import { registerTranslation } from "../src/i18n";
import { TRANSLATIONS } from "../src/i18n/all";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "heat_card");
/** The fixtures' moment: 16 November 2026, 17:20 in Stockholm. */
const NOW = Date.parse("2026-11-16T16:20:00+00:00");

function answer(name = "villa.json"): HeatAnswer {
  const decoded = decodeHeat(JSON.parse(readFileSync(join(FIXTURES, name), "utf8")));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

function withBoost(value: HeatAnswer, boost: HeatBoost | null): HeatAnswer {
  return { ...value, hot_water: { ...value.hot_water!, boost } };
}

const IDLE: HeatBoost = { state: "idle", source: null, started: null, ends: null, ends_estimated: false, stoppable: false };

beforeAll(() => {
  registerTranslation("sv", TRANSLATIONS.sv);
});

function render(value: HeatAnswer, tab: "now" | "plan" = "now", onBoost: ((action: "start" | "stop") => void) | null = vi.fn()) {
  const state: HeatState = { kind: "ready", answer: value };
  const handlers = { onStep: vi.fn(), onHome: vi.fn(), onAway: vi.fn(), onSettings: vi.fn(), ...(onBoost === null ? {} : { onBoost }) };
  return heatBody({ doc: document, language: "sv", state, ui: { pendingTarget: null, error: null, tab }, handlers, width: 360, nowMs: NOW });
}

describe("the Boost button", () => {
  it("is not drawn at all without a boost that ends by itself", () => {
    const card = render(withBoost(answer(), null));
    expect(card.querySelector("[data-block='boost']")).toBeNull();
    expect(render({ ...answer(), hot_water: null }).querySelector("[data-block='boost']")).toBeNull();
  });

  it("starts the hot water from idle and after a failure", () => {
    const onBoost = vi.fn();
    const card = render(withBoost(answer("network_pump.json"), IDLE), "now", onBoost);
    const button = card.querySelector<HTMLButtonElement>("[data-block='boost'] button")!;
    expect(button.textContent).toBe("Varmvatten nu");
    button.click();
    expect(onBoost).toHaveBeenCalledWith("start");
    const failed = render(withBoost(answer(), { ...IDLE, state: "failed" }));
    expect(failed.querySelector("[data-line='boost']")!.textContent).toBe("Kunde inte starta varmvattnet");
    expect(failed.querySelector("[data-block='boost'] button")!.textContent).toBe("Varmvatten nu");
  });

  it("says starting, then the time left, with a cancel only for the household's own boost that can be ended", () => {
    const starting = render(withBoost(answer(), { ...IDLE, state: "starting" }));
    const button = starting.querySelector<HTMLButtonElement>("[data-block='boost'] button")!;
    expect(button.textContent).toBe("Startar…");
    expect(button.disabled).toBe(true);
    const ends = new Date(NOW + 20 * 60_000).toISOString();
    const own = { state: "running", source: "user", started: new Date(NOW).toISOString(), ends, ends_estimated: true, stoppable: true };
    const onBoost = vi.fn();
    const running = render(withBoost(answer(), own), "now", onBoost);
    expect(running.querySelector("[data-line='boost']")!.textContent).toBe("Varmvatten · ca 20 min kvar");
    const cancel = running.querySelector<HTMLButtonElement>("[data-block='boost'] button")!;
    expect(cancel.textContent).toBe("Avbryt");
    cancel.click();
    expect(onBoost).toHaveBeenCalledWith("stop");
    // A timer's end is exact.
    expect(boostWords("sv", withBoost(answer(), { ...own, ends_estimated: false }), NOW)!.text).toBe("Varmvatten · 20 min kvar");
    // The villa's pulse: the household's run, no way to end it early, no button.
    const pulse = render(answer());
    expect(pulse.querySelector("[data-line='boost']")!.textContent).toMatch(/^Varmvatten · ca \d+ min kvar$/u);
    expect(pulse.querySelector("[data-block='boost'] button")).toBeNull();
    // SpotNav's own or the pump's own run: what it does, never a button.
    const plan = render(withBoost(answer(), { ...own, source: "plan", stoppable: false }));
    expect(plan.querySelector("[data-line='boost']")!.textContent).toBe("Värmer varmvatten · ca 20 min kvar");
    expect(plan.querySelector("[data-block='boost'] button")).toBeNull();
    const pump = boostWords("sv", withBoost(answer(), { ...IDLE, state: "running", source: "pump" }), NOW)!;
    expect(pump).toEqual({ text: "Värmer varmvatten", action: null, label: null });
  });

  it("is left out where the card cannot press it", () => {
    expect(render(withBoost(answer(), IDLE), "now", null).querySelector("[data-block='boost']")).toBeNull();
  });

  it("decodes the command's answer strictly", () => {
    expect(decodeHeatBoost({ api_version: 1, ok: true, error: null, boost: IDLE }).ok).toBe(true);
    expect(decodeHeatBoost({ api_version: 1, ok: false, error: "spotnav_rate_limited", boost: null }).ok).toBe(true);
    expect(decodeHeatBoost({ api_version: 1, ok: true, error: null, boost: { ...IDLE, extra: 1 } }).ok).toBe(false);
    expect(decodeHeatBoost({ api_version: 2, ok: true, error: null, boost: null })).toEqual({ ok: false, failure: "unsupported" });
  });
});

describe("the plan", () => {
  it("draws the planned boosts as hollow droplets with a bar, and the peaks as ticks", () => {
    const block = render(answer(), "plan").querySelector("[data-block='day']")!;
    expect(block.querySelectorAll("[data-mark='hot_water']")).toHaveLength(2);
    // Today's two would-be boosts (tomorrow's is not today's).
    expect(block.querySelectorAll("[data-mark='hot_water_planned']")).toHaveLength(2);
    expect(block.querySelectorAll("[data-mark='hot_water_bar']")).toHaveLength(2);
    const ticks = [...block.querySelectorAll("[data-mark='hot_water_peak']")].map((tick) => tick.textContent);
    expect(ticks).toEqual(["dusch 05:00", "dusch 17:00"]);
  });

  it("says each boost in words: what it would do, and what the pump did itself", () => {
    const value = answer();
    const [morning, evening, tomorrow] = value.hot_water!.planned;
    expect(plannedWaterText("sv", value, morning!)).toBe("Skulle ha värmt varmvatten 03:15 · pumpen körde själv 05:10");
    expect(plannedWaterText("sv", value, evening!)).toBe("Skulle ha värmt varmvatten 14:00");
    expect(plannedWaterText("sv", value, tomorrow!)).toBe("Skulle värma varmvatten 03:30–04:00 · 76 öre/kWh · klart till 05:00");
    expect(plannedWaterText("sv", value, { ...tomorrow!, live: true, reason: "sun" })).toBe("Varmvatten 03:30–04:00 · 76 öre/kWh · klart till 05:00 · på solel");
  });

  it("puts the strip's hot water at the planned boost's own time", () => {
    const value = answer();
    const ahead = value.ahead.map((hour) => (hour.startMs === Date.parse("2026-11-17T02:00:00+00:00") ? { ...hour, hot_water: true } : hour));
    const hours = [...ahead];
    while (hours.length < 12) {
      const last = hours[hours.length - 1]!;
      hours.push({ ...last, startMs: last.startMs + 3_600_000, start: new Date(last.startMs + 3_600_000).toISOString(), hot_water: last.startMs + 3_600_000 === Date.parse("2026-11-17T02:00:00+00:00") });
    }
    expect(aheadLine("sv", { ...value, ahead: hours })).toContain("varmvatten 03:30");
  });

  it("shows yesterday's review per peak and the gate's progress", () => {
    const lines = hotWaterLines("sv", answer());
    expect(lines.map((line) => line.key)).toEqual(["water-gate", "water-review", "water-peaks"]);
    expect(lines[0]!.text).toBe("Varmvatten i skuggning: dag 5 av 7, 8 av 10 toppar träffade");
    expect(lines[1]!.text).toBe("Varmvatten i går: 1,1 kWh flyttade · 0,61 kr sparade (skuggning)");
    expect(lines[2]!.text).toBe("Toppar: 05:00 träffad · skulle ha värmt varmvatten 03:15 · pumpen körde själv 05:10; 17:00 ingen tappning");
    const panel = render(answer(), "plan");
    expect(panel.querySelector("[data-line='water-gate']")).not.toBeNull();
    // Watch only (the pump's own price control): nothing about SpotNav's hot water.
    expect(hotWaterLines("sv", answer("network_pump.json"))).toEqual([]);
    const gate = answer().hot_water!.gate;
    const week = { ...answer(), hot_water: { ...answer().hot_water!, gate: { ...gate, days: 8, weekdays: 6 } } };
    expect(hotWaterLines("sv", week)[0]!.text).toBe("Varmvatten i skuggning: dag 7 av 7, 8 av 10 toppar träffade · 6 av 7 veckodagar");
    const ready = { ...answer(), hot_water: { ...answer().hot_water!, gate: { ...gate, open: true } } };
    expect(hotWaterLines("sv", ready)[0]!.text).toBe("Varmvatten: redo att styras med ”Styr värmen”");
  });
});
