// The battery plan's two fixed figures, read-only, under the mode: decoded tolerantly and said in the site's money.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { LANGUAGES } from "../src/i18n";
import { planCostsLine, planMoney } from "../src/settings-popover";
import { decodeBatteryControl, type BatteryStatus, type PlanCosts } from "../src/site-model";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "battery_control");
const SEK: PlanCosts = { wear_minor_per_kwh: 15, balanced_margin_minor_per_kwh: 100, major_unit: "kr", minor_unit: "öre" };

function status(change: (raw: Record<string, any>) => void = () => {}): BatteryStatus {
  const raw = JSON.parse(readFileSync(join(FIXTURES, "live.json"), "utf8")) as Record<string, any>;
  change(raw["status"]);
  const decoded = decodeBatteryControl(raw);
  if (!decoded.ok || decoded.value.status === null) {
    throw new Error("unreadable");
  }
  return decoded.value.status;
}

describe("the plan's fixed costs", () => {
  it("says them in the minor unit below one major unit and in the major one from there", () => {
    expect(planCostsLine("sv", status())).toBe("Slitage räknas som 15 öre/kWh åt varje håll · Balanserad säljer vid minst 1 kr/kWh netto");
    expect(planCostsLine("en", status())).toBe("Wear counted as 15 öre/kWh each way · Balanced sells at 1 kr/kWh net or more");
    expect(planMoney("en", 70, SEK)).toBe("70 öre");
    expect(planMoney("en", 1.4, { ...SEK, major_unit: "EUR", minor_unit: "cent" })).toBe("1.4 cent");
    expect(planMoney("en", 140, SEK)).toBe("1.40 kr");
    expect(planMoney("en", 0, SEK)).toBe("0 kr");
    expect(planMoney("en", 2500, SEK)).toBe("25 kr");
  });

  it("is silent without the block: older Home Assistant, or no price area", () => {
    expect(planCostsLine("en", status((raw) => delete raw["plan_costs"]))).toBeNull();
    expect(planCostsLine("en", status((raw) => (raw["plan_costs"] = null)))).toBeNull();
  });

  it("is told in every language with both figures", () => {
    for (const language of LANGUAGES) {
      const line = planCostsLine(language, status());
      expect(line, language).toMatch(/15/);
      expect(line, language).not.toContain("{");
    }
  });
});
