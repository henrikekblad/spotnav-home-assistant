// The battery plan's two figures in the mode dialog: decoded tolerantly and said in the site's money.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { LANGUAGES } from "../src/i18n";
import { planMarginLine, planMinor, planMoney, wearHelp, wearLabel, wearValue } from "../src/settings-popover";
import { decodeBatteryControl, type BatteryStatus, type PlanCosts } from "../src/site-model";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "battery_control");
const SEK: PlanCosts = {
  wear_minor_per_kwh: 15,
  wear_default_minor_per_kwh: 15,
  balanced_margin_minor_per_kwh: 100,
  major_unit: "kr",
  minor_unit: "öre",
};
const EUR: PlanCosts = {
  wear_minor_per_kwh: 1.4,
  wear_default_minor_per_kwh: 1.4,
  balanced_margin_minor_per_kwh: 10,
  major_unit: "EUR",
  minor_unit: "cent",
};

function status(change: (raw: Record<string, any>) => void = () => {}): BatteryStatus {
  const raw = JSON.parse(readFileSync(join(FIXTURES, "live.json"), "utf8")) as Record<string, any>;
  change(raw["status"]);
  const decoded = decodeBatteryControl(raw);
  if (!decoded.ok || decoded.value.status === null) {
    throw new Error("unreadable");
  }
  return decoded.value.status;
}

describe("the plan's costs", () => {
  it("says them in the minor unit below one major unit and in the major one from there", () => {
    expect(planMarginLine("sv", status())).toBe("Balanserad säljer vid minst 1 kr/kWh netto");
    expect(planMarginLine("en", status())).toBe("Balanced sells at 1 kr/kWh net or more");
    expect(planMoney("en", 70, SEK)).toBe("70 öre");
    expect(planMoney("en", 1.4, { ...SEK, major_unit: "EUR", minor_unit: "cent" })).toBe("1.4 cent");
    expect(planMoney("en", 140, SEK)).toBe("1.40 kr");
    expect(planMoney("en", 0, SEK)).toBe("0 kr");
    expect(planMoney("en", 2500, SEK)).toBe("25 kr");
  });

  it("is silent without the block: older Home Assistant, or no price area", () => {
    expect(planMarginLine("en", status((raw) => delete raw["plan_costs"]))).toBeNull();
    expect(planMarginLine("en", status((raw) => (raw["plan_costs"] = null)))).toBeNull();
  });

  it("reads an older Home Assistant's block and options without the wear's keys as no wear of the site's own", () => {
    const older = status((raw) => {
      delete raw["plan_costs"]["wear_default_minor_per_kwh"];
      delete raw["options"]["battery_wear_minor_per_kwh"];
    });
    expect(older.plan_costs!.wear_default_minor_per_kwh).toBeNull();
    expect(older.options.battery_wear_minor_per_kwh).toBeNull();
    expect(wearHelp("en", older.plan_costs!)).toBeNull();
    expect(status((raw) => (raw["options"]["battery_wear_minor_per_kwh"] = 22.5)).options.battery_wear_minor_per_kwh).toBe(22.5);
  });

  it("names the wear in the area's own minor unit, with the currency's default", () => {
    expect(wearLabel("sv", SEK)).toBe("Slitage (öre/kWh åt varje håll)");
    expect(wearHelp("sv", SEK)).toBe("Vad det kostar i slitage att ladda och ladda ur en kWh. Standard 15 öre.");
    expect(wearLabel("en", EUR)).toBe("Wear (cent/kWh each way)");
    expect(wearHelp("en", EUR)).toBe("What it costs in wear to charge and discharge one kWh. Default 1.4 cent.");
    expect(wearLabel("en", { ...SEK, minor_unit: "p" })).toBe("Wear (p/kWh each way)");
    expect(wearLabel("nb", { ...SEK, minor_unit: "øre" })).toBe("Slitasje (øre/kWh hver vei)");
    expect(planMinor("en", 540, { ...SEK, minor_unit: "fillér" })).toBe("540 fillér");
  });

  it("reads the wear field: empty is the default, a figure from 0 to ten times the default, anything else refused", () => {
    expect(wearValue("sv", "", SEK)).toEqual({ value: null });
    expect(wearValue("sv", " 22,5 ", SEK)).toEqual({ value: 22.5 });
    expect(wearValue("sv", "0", SEK)).toEqual({ value: 0 });
    expect(wearValue("sv", "150", SEK)).toEqual({ value: 150 });
    expect(wearValue("sv", "151", SEK)).toEqual({ error: "Mellan 0 och 150." });
    expect(wearValue("sv", "-1", SEK)).toEqual({ error: "Mellan 0 och 150." });
    expect(wearValue("sv", "mycket", SEK)).toEqual({ error: "Skriv ett tal." });
    expect(wearValue("en", "14", EUR)).toEqual({ value: 14 });
    expect(wearValue("en", "14.1", EUR)).toEqual({ error: "Between 0 and 14." });
  });

  it("is told in every language", () => {
    for (const language of LANGUAGES) {
      const line = planMarginLine(language, status());
      expect(line, language).toMatch(/1/);
      expect(line, language).not.toContain("{");
      expect(wearLabel(language, EUR), language).toContain("cent/kWh");
      expect(wearHelp(language, EUR), language).toMatch(/1[.,]4 cent/);
      expect(wearHelp(language, EUR), language).not.toContain("{");
    }
  });
});
