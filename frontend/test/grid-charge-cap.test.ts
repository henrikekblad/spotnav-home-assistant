// The battery status's `grid_charge_cap` block: decoded strictly, shown as one line under what SpotNav may do.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { LANGUAGES } from "../src/i18n";
import { gridChargeCapLine, importLimitHint, perPhaseLine } from "../src/settings-popover";
import { decodeBatteryControl, type BatteryStatus } from "../src/site-model";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "battery_control");
const fixture = (name: string): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, name), "utf8")) as Record<string, any>;

function status(cap: Record<string, unknown>, allowed = ["grid_charge"], more: Record<string, unknown> = {}): BatteryStatus {
  const raw = fixture("checking.json");
  raw["status"].allowed_actions = allowed;
  raw["status"].grid_charge_cap = { ...raw["status"].grid_charge_cap, ...cap };
  raw["status"] = { ...raw["status"], ...more };
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
      ready: false, usable: false, extra_margin_a: null, days_counted: 9, days_spread: 5, days_left: 9,
    };
    expect(gridChargeCapLine("sv", status({ rule: "conservative", cap_kw: 0, learned }))).toBe(
      "Ingen laddning från nätet: huset kan nå säkringen själv (försiktigt tills de första dygnen är inlärda, 2 dygn kvar).",
    );
    expect(gridChargeCapLine("en", status({ rule: "conservative", learned: { ...learned, days_spread: 6 } }))).toContain(
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
    // No charge at all: one reason. The learning's margin where the learned peaks alone would leave a charge
    // (the fixture's 3.2 kW), the house itself where they would not.
    expect(gridChargeCapLine("en", status({ rule: "learning", cap_kw: 0, learned }))).toBe(
      "No charging from the grid while the house's peaks are being learned (9 days left).",
    );
    expect(gridChargeCapLine("sv", status({ rule: "learning", cap_kw: 0, cap_learned_kw: 0, learned }))).toBe(
      "Ingen laddning från nätet: huset kan nå säkringen själv (inlärt per fas).",
    );
    for (const language of LANGUAGES) {
      const line = gridChargeCapLine(language, status({ rule: "learning", cap_kw: 0, learned }));
      expect(line, language).toMatch(/9/);
      expect(line, language).not.toContain("{");
    }
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
    const guarding = { value_kw: 4.0, max_kw: 4.47, suggested_kw: 4.4, holds: true, why: null };
    expect(gridChargeCapLine("sv", status({ cap_kw: null, rule: "import_limit", learned: null, import_limit: guarding }))).toBe(
      "Anläggningens egen importgräns (4 kW) skyddar säkringen vid laddning från nätet. Den begränsar summan över de tre"
        + " faserna och är därför satt lägre än säkringen: en fas kan bära husets topp ensam.",
    );
    // A Home Assistant that does not say the limit's value yet.
    expect(gridChargeCapLine("sv", status({ cap_kw: null, rule: "import_limit", learned: null, import_limit: null }))).toBe(
      "Anläggningens egen importgräns skyddar säkringen vid laddning från nätet.",
    );
    expect(gridChargeCapLine("sv", status({ cap_kw: null, rule: "error", learned: null }))).toBeNull();
    expect(gridChargeCapLine("sv", status({}, []))).toBeNull();
  });

  it("says why an import limit at the fuse does not guard it, and the most it may read", () => {
    const acts = { options: { ...fixture("checking.json")["status"].options, battery_control_import_limit_acts: true } };
    const tooHigh = { value_kw: 13.8, max_kw: 4.47, suggested_kw: 4.4, holds: false, why: null };
    expect(importLimitHint("sv", status({ import_limit: tooHigh }, ["grid_charge"], acts))).toBe(
      "Anläggningens importgräns står på 13,8 kW. Den begränsar summan över de tre faserna, inte varje fas, och skyddar"
        + " säkringen först vid högst 4,4 kW: en fas kan bära husets topp ensam.",
    );
    expect(importLimitHint("en", status({ import_limit: { ...tooHigh, value_kw: null } }, ["grid_charge"], acts))).toBe(
      "The plant's import limit limits the total over the three phases, not each phase. It protects the fuse when set"
        + " to 4.4 kW or less: one phase can carry the house's peak alone.",
    );
    // Nothing to say: it guards, it was never said to act, no value can guard, or no grid charging at all.
    expect(importLimitHint("sv", status({ import_limit: { ...tooHigh, holds: true } }, ["grid_charge"], acts))).toBeNull();
    expect(importLimitHint("sv", status({ import_limit: tooHigh }))).toBeNull();
    expect(importLimitHint("sv", status({ import_limit: { ...tooHigh, suggested_kw: null } }, ["grid_charge"], acts))).toBeNull();
    expect(importLimitHint("sv", status({ import_limit: null }, ["grid_charge"], acts))).toBeNull();
    expect(importLimitHint("sv", status({ import_limit: tooHigh }, ["hold"], acts))).toBeNull();
    for (const language of LANGUAGES) {
      for (const limit of [tooHigh, { ...tooHigh, value_kw: null }]) {
        const line = importLimitHint(language, status({ import_limit: limit }, ["grid_charge"], acts));
        expect(line, language).toMatch(/4[.,]4/);
        expect(line, language).not.toContain("{");
      }
      const total = gridChargeCapLine(language, status({ cap_kw: null, rule: "import_limit", import_limit: { ...tooHigh, value_kw: 4.2, holds: true } }));
      expect(total, language).toMatch(/4[.,]2/);
      expect(total, language).not.toContain("{");
    }
  });

  it("says a grid charge per phase: what each phase may take, and what it takes while one runs", () => {
    const block = { state: "verified", why: null, used: true, caps_kw: { L1: 1.02, L2: 0, L3: 0.14 }, charging_kw: null };
    expect(perPhaseLine("sv", status({}, ["grid_charge"], { per_phase: block }))).toBe(
      "Nätladdning per fas, högst: L1 1,02 kW · L2 0 kW · L3 0,14 kW",
    );
    const running = { ...block, charging_kw: { L1: 1.01, L2: 0, L3: 0.13 } };
    expect(perPhaseLine("sv", status({}, ["grid_charge"], { per_phase: running }))).toBe(
      "Nätladdning per fas: L1 1,01 kW · L2 0 kW · L3 0,13 kW",
    );
    // Not verified, not used now, no grid charging, or a plant without targets per phase: nothing.
    expect(perPhaseLine("sv", status({}, ["grid_charge"], { per_phase: { ...block, state: "pending" } }))).toBeNull();
    expect(perPhaseLine("sv", status({}, ["grid_charge"], { per_phase: { ...block, used: false } }))).toBeNull();
    expect(perPhaseLine("sv", status({}, ["hold"], { per_phase: running }))).toBeNull();
    expect(perPhaseLine("sv", status({}, ["grid_charge"], { per_phase: null }))).toBeNull();
    for (const language of LANGUAGES) {
      for (const each of [block, running]) {
        const line = perPhaseLine(language, status({}, ["grid_charge"], { per_phase: each }));
        expect(line, language).toMatch(/L1 1[.,]0[12] kW · L2 0 kW · L3 0[.,]1[34] kW/);
        expect(line, language).not.toContain("{");
      }
    }
  });

  it("reads an answer from before the two blocks, and rejects a mistyped one", () => {
    const older = fixture("checking.json");
    delete older["status"].per_phase;
    delete older["status"].grid_charge_cap.import_limit;
    const decoded = decodeBatteryControl(older);
    expect(decoded.ok).toBe(true);
    if (decoded.ok) {
      expect(decoded.value.status?.per_phase).toBeNull();
      expect(decoded.value.status?.grid_charge_cap.import_limit).toBeNull();
    }
    const mistyped = fixture("checking.json");
    mistyped["status"].per_phase = { state: "verified", why: null, used: true, caps_kw: { L1: 1, L2: 0 }, charging_kw: null };
    expect(decodeBatteryControl(mistyped).ok).toBe(false);
    const extra = fixture("checking.json");
    extra["status"].grid_charge_cap.import_limit = { value_kw: 1, max_kw: 1, suggested_kw: 1, holds: false, why: null, more: 1 };
    expect(decodeBatteryControl(extra).ok).toBe(false);
  });

  it("has the line in every language", () => {
    for (const language of LANGUAGES) {
      const line = gridChargeCapLine(language, status({}));
      expect(line, language).toMatch(/3[.,]2/);
      expect(line, language).not.toContain("{");
    }
  });
});
