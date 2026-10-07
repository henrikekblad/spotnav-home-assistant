// The car's two percent sliders: the charge target (0..100) and the minimum charge level (Off, then 10..80 in
// fives, never past the target), and the minimum's shaded segment on the plan's target slider.

import { describe, expect, it } from "vitest";

import {
  FLOOR_STOPS,
  clampFloorIndex,
  defaultTargetPercent,
  floorAt,
  floorCapIndex,
  floorIndex,
  floorSegment,
} from "../src/percent-slider";

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
