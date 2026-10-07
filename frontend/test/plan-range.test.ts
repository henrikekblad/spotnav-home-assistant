// The Plan editor's current range: the charger's own, decoded independently from dashboard v7, judged
// only when the reader moved the value, and never wider than the contract's own 80 A bound.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { DEFAULT_CURRENT_RANGE, currentRangeFor } from "../src/model";
import { checkCurrentInRange, formFromRecord, planSummaryParts, replacementFor } from "../src/settings";
import { decodeDashboard, type Dashboard } from "../src/validate";
import type { SettingsRecord } from "../src/types";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");

function decoded(name: string, patch: (raw: Record<string, unknown>) => void = () => {}): Dashboard {
  const raw = JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
  patch(raw);
  const result = decodeDashboard(raw);
  if (!result.ok) {
    throw new Error(`${name}: ${result.failure}`);
  }
  return result.value;
}

const record: SettingsRecord = {
  revision: 3,
  area_id: "SE4",
  overrides: [],
  phases: 3,
  amps: 16,
  requested_kwh: 20,
  max_periods: 3,
  departure_enabled: false,
  departure_time: "07:00",
  departure_date: null,
  departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
  strategy: "cheapest",
  driver: "manual_kwh",
  target: { vehicle_id: null, target_percent: null },
};

describe("the range from the dashboard", () => {
  it("is the charger's own when it states one", () => {
    expect(currentRangeFor(decoded("charger_states_its_maximum"))).toEqual({ minA: 6, maxA: 16 });
  });

  it("is the everyday range for a charger that states none", () => {
    expect(currentRangeFor(decoded("cheapest_no_site"))).toEqual({ minA: 6, maxA: 32 });
  });

  it.each([
    ["null", (raw: Record<string, unknown>) => (raw["current_range"] = null)],
    ["above the contract's bound", (raw: Record<string, unknown>) => (raw["current_range"] = { min_a: 6, max_a: 81, source: "x" })],
    ["upside down", (raw: Record<string, unknown>) => (raw["current_range"] = { min_a: 20, max_a: 10, source: "x" })],
    ["fractional", (raw: Record<string, unknown>) => (raw["current_range"] = { min_a: 6, max_a: 16.5, source: "x" })],
    ["with a stray key", (raw: Record<string, unknown>) => (raw["current_range"] = { min_a: 6, max_a: 16, source: "x", more: 1 })],
  ])("falls back to the everyday range, and only that, when it is %s", (_name, patch) => {
    const dashboard = decoded("charger_states_its_maximum", patch);
    expect(currentRangeFor(dashboard)).toEqual(DEFAULT_CURRENT_RANGE);
    expect("strategy" in dashboard && dashboard.strategy.selected).toBe("cheapest");
  });
});

describe("judging the current", () => {
  it("accepts the ends and refuses beyond them", () => {
    const range = { minA: 6, maxA: 16 };
    expect(checkCurrentInRange("6", range)).toEqual({ ok: true, value: 6 });
    expect(checkCurrentInRange("16", range)).toEqual({ ok: true, value: 16 });
    expect(checkCurrentInRange("17", range).ok).toBe(false);
    expect(checkCurrentInRange("5", range).ok).toBe(false);
  });

  it("asks only when the reader moved the value", () => {
    const range = { minA: 6, maxA: 10 };
    const untouched = { ...formFromRecord(record), energy: "25" };
    // 16 A is stored and above this charger's 10 A: not the reader's change, so a neighbouring Save goes.
    expect(replacementFor("plan", record, untouched, range).ok).toBe(true);
    const moved = { ...untouched, current: "12" };
    expect(replacementFor("plan", record, moved, range).ok).toBe(false);
    expect(replacementFor("plan", record, { ...untouched, current: "9" }, range).ok).toBe(true);
  });

  it("saves the three planning values in one replacement, and nothing else", () => {
    const values = { ...formFromRecord(record), energy: "30", deadlineEnabled: true, deadlineTime: "06:15", current: "10" };
    const check = replacementFor("plan", record, values, { minA: 6, maxA: 32 });
    expect(check.ok && check.changed).toBe(true);
    if (check.ok) {
      expect(check.body).toMatchObject({
        requested_kwh: 30,
        departure_enabled: true,
        departure_time: "06:15",
        departure_date: null,
        departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
        // The charge periods are a charger setting, kept as stored.
        max_periods: 3,
        amps: 10,
        phases: 3,
      });
    }
    const same = replacementFor("plan", record, formFromRecord(record), { minA: 6, maxA: 32 });
    expect(same.ok && same.changed).toBe(false);
  });

  it("writes the Plan cell as one line of three parts", () => {
    expect(planSummaryParts("en", null)).toEqual(["Not set", "No deadline", "Not set"]);
  });
});
