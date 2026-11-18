// The battery status's `grid_charge_cap` block: decoded strictly, shown as one line under what SpotNav may do.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { LANGUAGES } from "../src/i18n";
import { gridChargeCapLine } from "../src/settings-popover";
import { decodeBatteryControl, type BatteryStatus } from "../src/site-model";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "battery_control");
const fixture = (name: string): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, name), "utf8")) as Record<string, any>;

function status(cap: Record<string, unknown>, allowed = ["grid_charge"]): BatteryStatus {
  const raw = fixture("checking.json");
  raw["status"].allowed_actions = allowed;
  raw["status"].grid_charge_cap = { ...raw["status"].grid_charge_cap, ...cap };
  const decoded = decodeBatteryControl(raw);
  if (!decoded.ok || decoded.value.status === null) {
    throw new Error("unreadable");
  }
  return decoded.value.status;
}

describe("the grid charge cap block", () => {
  it("reads both fixtures, the null forms included", () => {
    expect(decodeBatteryControl(fixture("checking.json")).ok).toBe(true);
    expect(decodeBatteryControl(fixture("needs_ack.json")).ok).toBe(true);
  });

  it("rejects a missing block, an unknown key and a mistyped nested value", () => {
    const missing = fixture("checking.json");
    delete missing["status"].grid_charge_cap;
    expect(decodeBatteryControl(missing).ok).toBe(false);
    const extra = fixture("checking.json");
    extra["status"].grid_charge_cap.extra = 1;
    expect(decodeBatteryControl(extra).ok).toBe(false);
    const nested = fixture("checking.json");
    nested["status"].grid_charge_cap.learned.raw_a.L2 = "4";
    expect(decodeBatteryControl(nested).ok).toBe(false);
  });

  it("says the cap and the rule in Swedish", () => {
    expect(gridChargeCapLine("sv", status({}))).toBe(
      "Högst 3,2 kW från nätet, så säkringen klarar sig även om Home Assistant stannar (inlärt per fas).",
    );
    const few = { ...fixture("checking.json")["status"].grid_charge_cap.learned, ready: false, usable: false, days_counted: 3 };
    expect(gridChargeCapLine("sv", status({ rule: "conservative", learned: null }))).toContain(
      "(försiktigt tills de första dygnen är inlärda)",
    );
    expect(few.days_min).toBe(7);
  });

  it("says the days left to the first usable days while the cautious rule counts", () => {
    const learned = {
      ...fixture("checking.json")["status"].grid_charge_cap.learned,
      ready: false, usable: false, extra_margin_a: null, days_counted: 5, days_left: 9,
    };
    expect(gridChargeCapLine("sv", status({ rule: "conservative", cap_kw: 0, learned }))).toBe(
      "Ingen laddning från nätet: huset kan nå säkringen själv (försiktigt tills de första dygnen är inlärda, 2 dygn kvar).",
    );
    expect(gridChargeCapLine("en", status({ rule: "conservative", learned: { ...learned, days_counted: 6 } }))).toContain(
      "(cautious until the first days are learned, 1 day left)",
    );
  });

  it("says the days left to the whole learning while the learned peaks carry a further margin", () => {
    const learned = {
      ...fixture("checking.json")["status"].grid_charge_cap.learned,
      ready: false, usable: true, extra_margin_a: 3.27, days_counted: 8, days_left: 9, days_from_history: 5,
    };
    expect(gridChargeCapLine("sv", status({ rule: "learning", cap_kw: 1.44, learned }))).toBe(
      "Högst 1,4 kW från nätet, så säkringen klarar sig även om Home Assistant stannar (medan husets toppar lärs in, 9 dygn kvar).",
    );
    expect(gridChargeCapLine("en", status({ rule: "learning", cap_kw: 0, learned }))).toContain(
      "(while the house's peaks are being learned, 9 days left)",
    );
    for (const language of LANGUAGES) {
      const line = gridChargeCapLine(language, status({ rule: "learning", cap_kw: 1.44, learned }));
      expect(line, language).toMatch(/1[.,]4.*9/);
      expect(line, language).not.toContain("{");
    }
  });

  it("says when the house can reach the fuse by itself", () => {
    expect(gridChargeCapLine("sv", status({ cap_kw: 0 }))).toBe(
      "Ingen laddning från nätet: huset kan nå säkringen själv (inlärt per fas).",
    );
  });

  it("names the import limit when there is no cap, and shows nothing when it cannot", () => {
    expect(gridChargeCapLine("sv", status({ cap_kw: null, rule: "import_limit", learned: null }))).toBe(
      "Importgränsen skyddar säkringen vid laddning från nätet.",
    );
    expect(gridChargeCapLine("sv", status({ cap_kw: null, rule: "error", learned: null }))).toBeNull();
    expect(gridChargeCapLine("sv", status({}, []))).toBeNull();
  });

  it("has the line in every language", () => {
    for (const language of LANGUAGES) {
      const line = gridChargeCapLine(language, status({}));
      expect(line, language).toMatch(/3[.,]2/);
      expect(line, language).not.toContain("{");
    }
  });
});
