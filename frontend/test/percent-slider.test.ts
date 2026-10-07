// The car's two percent sliders: the charge target (0..100) and the minimum charge level (Off, then 10..80 in
// fives, never past the target), and the minimum's shaded segment on the plan's target slider.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  FLOOR_STOPS,
  clampFloorIndex,
  defaultTargetPercent,
  floorAt,
  floorCapIndex,
  floorIndex,
  floorSegment,
  limitOpening,
  limitStops,
} from "../src/percent-slider";
import { decodeVehicle } from "../src/validate";

describe("the minimum's stops", () => {
  it("are Off, then 10 to 80 in fives", () => {
    expect(FLOOR_STOPS).toEqual([null, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80]);
  });

  it("map a level to its stop and back", () => {
    expect(floorIndex(null)).toBe(0);
    expect(floorIndex(10)).toBe(1);
    expect(floorIndex(30)).toBe(5);
    expect(floorIndex(80)).toBe(15);
    expect(floorAt(0)).toBeNull();
    expect(floorAt(5)).toBe(30);
    expect(floorAt(15)).toBe(80);
    expect(floorAt(99)).toBe(80);
    expect(floorAt(-1)).toBeNull();
  });
});

describe("the minimum is capped by the target", () => {
  it("stops at the highest level not above the target", () => {
    expect(floorCapIndex(80)).toBe(15);
    expect(floorCapIndex(100)).toBe(15);
    expect(floorCapIndex(50)).toBe(floorIndex(50));
    expect(floorCapIndex(52)).toBe(floorIndex(50));
    expect(floorCapIndex(10)).toBe(1);
    expect(floorCapIndex(9)).toBe(0);
    expect(floorCapIndex(0)).toBe(0);
  });

  it("clamps a move past the target back to it, and leaves Off alone", () => {
    expect(clampFloorIndex(15, 50)).toBe(floorIndex(50));
    expect(clampFloorIndex(3, 50)).toBe(3);
    expect(clampFloorIndex(0, 5)).toBe(0);
    expect(clampFloorIndex(4, 5)).toBe(0);
  });
});

describe("the target with none stored", () => {
  it("is the backend's default, 80 %, no higher than the car's own limit", () => {
    expect(defaultTargetPercent(null)).toBe(80);
    expect(defaultTargetPercent(90)).toBe(80);
    expect(defaultTargetPercent(70.6)).toBe(70);
  });
});

describe("the minimum's segment on the plan's target slider", () => {
  it("runs from 0 to the minimum, its label in the middle of it", () => {
    expect(floorSegment(30, 80)).toEqual({ percent: 30, end: 0.3, label: 0.15 });
  });

  it("is never drawn past the target being chosen", () => {
    expect(floorSegment(50, 40)).toEqual({ percent: 40, end: 0.4, label: 0.2 });
  });

  it("is absent when the minimum is off, or nothing is left of it", () => {
    expect(floorSegment(null, 80)).toBeNull();
    expect(floorSegment(30, 0)).toBeNull();
  });

  it("uses the minimum alone when the target is not known", () => {
    expect(floorSegment(30, null)).toEqual({ percent: 30, end: 0.3, label: 0.15 });
  });
});

describe("the charge limit's stops", () => {
  it("are the range and step the car's limit reports", () => {
    expect(limitStops({ min: 50, max: 100, step: 10 })).toEqual({ min: 50, max: 100, step: 10 });
  });

  it("fall back to 1..100 in whole percent only when unknown", () => {
    expect(limitStops(null)).toEqual({ min: 1, max: 100, step: 1 });
    expect(limitStops(undefined)).toEqual({ min: 1, max: 100, step: 1 });
  });

  it("open a limit at its own stop, or the nearest one inside the range", () => {
    const kia = { min: 50, max: 100, step: 10 };
    expect(limitOpening(80, kia)).toBe(80);
    expect(limitOpening(84, kia)).toBe(80);
    expect(limitOpening(86, kia)).toBe(90);
    expect(limitOpening(30, kia)).toBe(50);
    expect(limitOpening(100, kia)).toBe(100);
    expect(limitOpening(95, { min: 50, max: 95, step: 10 })).toBe(90);
    expect(limitOpening(72.4, limitStops(null))).toBe(72);
  });
});

describe("the vehicle row's charge limit range", () => {
  const row = JSON.parse(
    readFileSync(join(__dirname, "..", "..", "tests", "fixtures", "dashboard", "target_soc_two_vehicles.json"), "utf8"),
  ).vehicles[0];

  it("is decoded when present, null when unknown, and absent from an older backend", () => {
    expect(decodeVehicle({ ...row, charge_limit_range: { min: 50, max: 100, step: 10 } }).charge_limit_range).toEqual({
      min: 50,
      max: 100,
      step: 10,
    });
    expect(decodeVehicle({ ...row, charge_limit_range: null }).charge_limit_range).toBeNull();
    const { charge_limit_range: _, ...older } = row;
    expect("charge_limit_range" in decodeVehicle(older)).toBe(false);
  });

  it("refuses a range no limit can have", () => {
    for (const bad of [
      { min: 0, max: 100, step: 1 },
      { min: 50, max: 110, step: 10 },
      { min: 80, max: 50, step: 10 },
      { min: 50, max: 100, step: 0 },
      { min: 50, max: 100 },
      { min: 50, max: 100, step: 10, extra: 1 },
    ]) {
      expect(() => decodeVehicle({ ...row, charge_limit_range: bad })).toThrow();
    }
  });
});
