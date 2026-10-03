// The card's target-energy computation against the backend's own, and the formula's edges.

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { chargeCeiling, effectiveTarget, pythonRound, targetNeedKwh } from "../src/target-need";
import { decodeDashboard } from "../src/validate";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");

const base = { soc: 40, capacityKwh: 77, targetPercent: 80, maxPercent: null, efficiency: 0.9 };

describe("the target need, as the backend computes it", () => {
  it("is (min(target, max) - soc) x capacity / efficiency", () => {
    expect(targetNeedKwh(base)).toBeCloseTo((40 / 100) * (77 / 0.9), 10);
    expect(targetNeedKwh({ ...base, maxPercent: 60 })).toBeCloseTo((20 / 100) * (77 / 0.9), 10);
  });

  it("is zero at or below the current level, and unknown without a battery size or a reading", () => {
    expect(targetNeedKwh({ ...base, targetPercent: 40 })).toBe(0);
    expect(targetNeedKwh({ ...base, targetPercent: 10 })).toBe(0);
    expect(targetNeedKwh({ ...base, maxPercent: 30 })).toBe(0);
    expect(targetNeedKwh({ ...base, capacityKwh: null })).toBeNull();
    expect(targetNeedKwh({ ...base, capacityKwh: 0 })).toBeNull();
    expect(targetNeedKwh({ ...base, soc: null })).toBeNull();
  });

  it("takes the target as a whole percent the way Python rounds it, and the limit as a whole percent down", () => {
    expect([80.5, 81.5, 80.4, 80.6, 0.5, 1.5].map(pythonRound)).toEqual([80, 82, 80, 81, 0, 2]);
    expect(effectiveTarget(80.5, null)).toBe(80);
    expect(chargeCeiling(80.9)).toBe(80);
    expect(chargeCeiling(null)).toBe(100);
    expect(chargeCeiling(140)).toBe(100);
    expect(chargeCeiling(-3)).toBe(0);
  });
});

describe("the card's computation against the backend's need_kwh in the committed fixtures", () => {
  const carrying = readdirSync(DASHBOARD_DIR)
    .sort()
    .filter((name) => {
      const soc = (JSON.parse(readFileSync(join(DASHBOARD_DIR, name), "utf8")) as { soc: { need_kwh: number | null } | null }).soc;
      return soc !== null && soc.need_kwh !== null;
    });

  it("finds the fixtures that carry a need", () => {
    expect(carrying).toEqual([
      "target_soc_estimated.json",
      "target_soc_phases_limited_by_vehicle.json",
      "target_soc_stopped_on_estimate.json",
      "target_soc_two_vehicles.json",
    ]);
  });

  it.each(carrying)("gives the need %s states, from the facts the same block states", (name) => {
    const decoded = decodeDashboard(JSON.parse(readFileSync(join(DASHBOARD_DIR, name), "utf8")));
    if (!decoded.ok || decoded.value.soc === null) {
      throw new Error(`${name} does not decode`);
    }
    const soc = decoded.value.soc;
    const computed = targetNeedKwh({
      soc: soc.value,
      capacityKwh: soc.capacity_kwh,
      targetPercent: soc.target_percent as number,
      maxPercent: soc.vehicle_max_percent,
      efficiency: soc.efficiency,
    }) as number;
    // The block states its reading to one decimal while the backend computed from the unrounded one,
    // so the card can be off by at most half a tenth of a percent of the battery, over the efficiency.
    const rounding = (0.05 / 100) * (soc.capacity_kwh as number) / soc.efficiency;
    expect(Math.abs(computed - (soc.need_kwh as number))).toBeLessThanOrEqual(rounding + 0.005);
  });

  it("is exactly the backend's figure, to its two decimals, when the reading has no rounding to lose", () => {
    const decoded = decodeDashboard(JSON.parse(readFileSync(join(DASHBOARD_DIR, "target_soc_two_vehicles.json"), "utf8")));
    if (!decoded.ok || decoded.value.soc === null) {
      throw new Error("no soc");
    }
    const soc = decoded.value.soc;
    const computed = targetNeedKwh({
      soc: soc.value,
      capacityKwh: soc.capacity_kwh,
      targetPercent: soc.target_percent as number,
      maxPercent: soc.vehicle_max_percent,
      efficiency: soc.efficiency,
    }) as number;
    expect(Math.round(computed * 100) / 100).toBe(soc.need_kwh);
  });
});
