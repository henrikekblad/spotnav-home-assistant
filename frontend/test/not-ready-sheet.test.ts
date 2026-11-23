// What will not be ready in time across the site (`src/not-ready-sheet.ts`): `get_plan`'s `not_ready` (the backend's
// contract fixture `tests/fixtures/plan/not_ready.json`: the garage's car 3 kWh short of 07:00 and the spa's 19:00 bath
// reaching 35 °C, both under the 6 kW import target) read, worded and drawn as the choice sheet in the house card's
// Plan tab and in the heat card's Spa and Sauna tabs, each press sent where that load's own line sends it.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeAll, describe, expect, it, vi } from "vitest";

import { decodeHeat, type HeatAnswer } from "../src/heat-model";
import { heatBody, type HeatHandlers } from "../src/heat-view";
import { decodePlan, planBody, type PlanAnswer } from "../src/house-plan";
import { registerTranslation } from "../src/i18n";
import { TRANSLATIONS } from "../src/i18n/all";
import { eventOptions } from "../src/notifications";
import {
  decodeNotReady,
  NOT_READY_HASH,
  notReadyButtons,
  notReadyLine,
  notReadyOrNull,
  notReadySheet,
  openedForNotReady,
  type NotReady,
  type NotReadyPress,
} from "../src/not-ready-sheet";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const SV = { language: "sv" as const, timeZone: "Europe/Stockholm", unit: "kWh", currency: "SEK", majorUnit: "kr" };
const EN = { ...SV, language: "en" as const };
const MONEY = { major_unit: "kr", minor_unit: "öre" };

function planRaw(name = "not_ready.json"): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, "plan", name), "utf8")) as Record<string, unknown>;
}

function plan(name = "not_ready.json"): PlanAnswer {
  const decoded = decodePlan(planRaw(name));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

function block(): NotReady {
  return plan().not_ready!;
}

beforeAll(() => {
  registerTranslation("sv", TRANSLATIONS.sv);
});

describe("the block", () => {
  it("reads the backend's block, and null where all fits", () => {
    const value = block();
    expect(value.items.map((item) => [item.kind, item.load_id])).toEqual([
      ["car", "charger_garage"],
      ["spa", "spa"],
    ]);
    expect(value.ranked[0]).toEqual({ load_id: null, code: "accept_peak", frees_kwh: 9 });
    expect(plan("villa.json").not_ready).toBeNull();
  });

  it("refuses a key it does not know, and the plan with it", () => {
    const raw = planRaw();
    const notReady = raw["not_ready"] as Record<string, unknown>;
    expect(() => decodeNotReady({ ...notReady, extra: 1 })).toThrow();
    expect(notReadyOrNull({ ...notReady, extra: 1 })).toBeNull();
    expect(decodePlan({ ...raw, not_ready: { ...notReady, extra: 1 } }).ok).toBe(false);
  });

  it("says each load's line", () => {
    const [car, spa] = block().items;
    expect(notReadyLine(SV, car!)).toBe("Garage 07:00: 3,0 kWh saknas");
    expect(notReadyLine(SV, spa!)).toBe("Spa 19:00: når 35 °C");
    expect(notReadyLine(EN, spa!)).toBe("Spa 19:00: reaches 35 °C");
  });

  it("offers every choice, the most freed first, each named by its load; the peak once for all", () => {
    const buttons = notReadyButtons(SV, block(), MONEY);
    expect(buttons.map((button) => button.label)).toEqual([
      "Tillåt toppen för alla",
      "Spa: Senare badtid (20:00)",
      "Spa: Ta det som ryms (35 °C)",
      "Garage: Senare avresa (08:00)",
      "Garage: Ta det som ryms (≈ 14,8 kWh)",
    ]);
    expect(buttons[0]!.press).toEqual({
      code: "accept_peak",
      loads: [
        { load_id: "charger_garage", kind: "car" },
        { load_id: "spa", kind: "spa" },
      ],
    });
  });

  it("a load alone keeps its own words, the peak its own figures", () => {
    const value = block();
    const alone: NotReady = {
      items: [value.items[0]!],
      ranked: [
        { load_id: null, code: "accept_peak", frees_kwh: 3 },
        { load_id: "charger_garage", code: "later", frees_kwh: 3 },
      ],
    };
    expect(notReadyButtons(SV, alone, MONEY).map((button) => button.label)).toEqual([
      "Tillåt toppen (≈ 13 kW, ≈ 45 kr)",
      "Senare avresa (08:00)",
    ]);
  });
});

describe("the sheet", () => {
  it("asks again before the peak, and sends each press once", () => {
    const pressed: NotReadyPress[] = [];
    const sheet = notReadySheet(document, SV, block(), MONEY, (press) => pressed.push(press));
    expect(sheet.id).toBe(NOT_READY_HASH.slice(1));
    expect([...sheet.querySelectorAll("li")].map((item) => item.textContent)).toEqual(["Garage 07:00: 3,0 kWh saknas", "Spa 19:00: når 35 °C"]);
    const buttons = [...sheet.querySelectorAll<HTMLButtonElement>("button")];
    buttons[0]!.click();
    expect(pressed).toEqual([]);
    expect(buttons[0]!.textContent).toBe("Ja, tillåt toppen");
    buttons[0]!.click();
    buttons[2]!.click();
    expect(pressed.map((press) => press.code)).toEqual(["accept_peak", "takes_what_fits"]);
    expect(buttons[2]!.disabled).toBe(true);
  });

  it("has no buttons without a handler", () => {
    expect(notReadySheet(document, SV, block(), MONEY).querySelectorAll("button")).toHaveLength(0);
  });

  it("stands first in the house card's Plan tab, its presses to the handler", () => {
    const onNotReady = vi.fn();
    const root = planBody({ doc: document, language: "sv", plan: plan(), width: 360, onNotReady });
    const sheet = root.querySelector<HTMLElement>("[data-part='not-ready']");
    expect(sheet).not.toBeNull();
    sheet!.querySelectorAll<HTMLButtonElement>("button")[1]!.click();
    expect(onNotReady).toHaveBeenCalledWith({ code: "later", loads: [{ load_id: "spa", kind: "spa" }] });
    expect(planBody({ doc: document, language: "sv", plan: plan("villa.json"), width: 360 }).querySelector("[data-part='not-ready']")).toBeNull();
  });

  it("knows the fragment a tap on the phones' notice opens the page with", () => {
    window.location.hash = NOT_READY_HASH;
    expect(openedForNotReady()).toBe(true);
    window.location.hash = "";
    expect(openedForNotReady()).toBe(false);
  });
});

describe("the heat card's Spa and Sauna tabs", () => {
  function heat(): HeatAnswer {
    const decoded = decodeHeat(JSON.parse(readFileSync(join(FIXTURES, "heat_card", "controlled.json"), "utf8")));
    if (!decoded.ok) {
      throw new Error("the fixture must decode");
    }
    return decoded.value;
  }

  function sauna(notReady: NotReady | null, extra: Partial<HeatHandlers> = {}): HTMLElement {
    return heatBody({
      doc: document,
      language: "sv",
      state: { kind: "ready", answer: heat() },
      ui: { pendingTarget: null, error: null, tab: "sauna" },
      handlers: { onStep: vi.fn(), onHome: vi.fn(), onAway: vi.fn(), onSettings: vi.fn(), ...extra },
      width: 330,
      nowMs: Date.parse("2026-11-16T16:20:00+00:00"),
      idPrefix: "t",
      notReady,
    });
  }

  it("show the site's sheet where more than their own session will not be ready, else their own choices", () => {
    const value = block();
    const saunaItem = { ...value.items[1]!, kind: "sauna" as const, load_id: "sauna" };
    const both: NotReady = { items: [value.items[0]!, saunaItem], ranked: [{ load_id: "sauna", code: "takes_what_fits", frees_kwh: 6 }] };
    const onNotReady = vi.fn();
    const root = sauna(both, { onNotReady });
    expect(root.querySelector("[data-part='not-ready']")).not.toBeNull();
    expect(root.querySelector("[data-block='sauna-not-ready']")).toBeNull();
    root.querySelector<HTMLButtonElement>("[data-part='not-ready'] button")!.click();
    expect(onNotReady).toHaveBeenCalledWith({ code: "takes_what_fits", loads: [{ load_id: "sauna", kind: "sauna" }] });
    const own = sauna({ items: [saunaItem], ranked: [] });
    expect(own.querySelector("[data-part='not-ready']")).toBeNull();
    expect(own.querySelector("[data-block='sauna-not-ready']")).not.toBeNull();
    expect(sauna(null).querySelector("[data-block='sauna-not-ready']")).not.toBeNull();
  });
});

describe("the notification setting", () => {
  it("names plan_at_risk as not ready in time, in every language", () => {
    expect(eventOptions("sv").find((option) => option.value === "plan_at_risk")?.label).toBe("Blir inte klart i tid");
    expect(eventOptions("en").find((option) => option.value === "plan_at_risk")?.label).toBe("Not ready in time");
    for (const language of Object.keys(TRANSLATIONS) as (keyof typeof TRANSLATIONS)[]) {
      expect(TRANSLATIONS[language]["notifications.event.planAtRisk"], language).toBe(TRANSLATIONS[language]["heat.settings.event.plan_at_risk"]);
    }
  });
});
