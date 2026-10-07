// The car's percent sliders, as numbers only: the charge target (0..100, whole percent), the minimum charge level
// (Off, then 10..80 in fives), which never goes past the target, as the backend's `effective_floor` caps it, and the
// car's own charge limit (the range and step its integration takes). Also where the minimum's shaded segment sits
// on the plan's target slider, with the ticks for the level now and the car's limit and where their words go.

import { CHARGE_LIMIT_MAX_PERCENT, CHARGE_LIMIT_MIN_PERCENT } from "./settings";
import { chargeCeiling } from "./target-need";
import { MIN_PERCENT_HIGH, MIN_PERCENT_LOW, MIN_PERCENT_STEP, type ChargeLimitRange } from "./validate";

/** The car's own limit with no range from its integration: whole percent, 1..100; the write refuses the rest. */
export const LIMIT_FALLBACK: ChargeLimitRange = { min: CHARGE_LIMIT_MIN_PERCENT, max: CHARGE_LIMIT_MAX_PERCENT, step: 1 };

/** The charge limit slider's range and step: the car's limit's own, else the fallback. */
export function limitStops(range: ChargeLimitRange | null | undefined): ChargeLimitRange {
  return range ?? LIMIT_FALLBACK;
}

/** The stop a limit opens at: its own, or the nearest one inside the range (never past the last whole step). */
export function limitOpening(current: number, range: ChargeLimitRange): number {
  const last = Math.floor((range.max - range.min) / range.step + 1e-9);
  const index = Math.min(last, Math.max(0, Math.round((current - range.min) / range.step)));
  // Rounded to keep 0.1-steps from drifting into binary noise.
  return Math.round((range.min + index * range.step) * 1e6) / 1e6;
}

/** The backend's target for a car with none stored (`planner.DEFAULT_TARGET_SOC_PERCENT`). */
export const DEFAULT_TARGET_PERCENT = 80;

/** The minimum's stops, left to right: Off, then each level. */
export const FLOOR_STOPS: readonly (number | null)[] = (() => {
  const stops: (number | null)[] = [null];
  for (let level = MIN_PERCENT_LOW; level <= MIN_PERCENT_HIGH; level += MIN_PERCENT_STEP) {
    stops.push(level);
  }
  return stops;
})();

export const FLOOR_LAST_INDEX = FLOOR_STOPS.length - 1;

/** The stop a level stands at: Off is 0, a level between stops goes to the one below it. */
export function floorIndex(value: number | null): number {
  if (value === null || !Number.isFinite(value) || value < MIN_PERCENT_LOW) {
    return 0;
  }
  const index = Math.floor((value - MIN_PERCENT_LOW) / MIN_PERCENT_STEP + 1e-9) + 1;
  return Math.min(FLOOR_LAST_INDEX, index);
}

/** The level at a stop (`null` for Off), clamped to the ends. */
export function floorAt(index: number): number | null {
  const clamped = Math.min(FLOOR_LAST_INDEX, Math.max(0, Math.round(index)));
  return FLOOR_STOPS[clamped] ?? null;
}

/** The last stop not above `target`: the furthest the minimum can go. */
export function floorCapIndex(target: number): number {
  return floorIndex(target);
}

/** A stop moved to, held back at the target. */
export function clampFloorIndex(index: number, target: number): number {
  return Math.max(0, Math.min(index, floorCapIndex(target)));
}

/** The target a car with none stored is planned with: the default, no higher than its own limit. */
export function defaultTargetPercent(maxPercent: number | null): number {
  return Math.min(DEFAULT_TARGET_PERCENT, chargeCeiling(maxPercent));
}

/**
 * The minimum on a 0..100 % slider: shaded from 0 to it (no further than `target`, as the backend applies it),
 * its label under the middle of the shading. Fractions of the track; `null` when there is nothing to shade.
 */
export function floorSegment(
  floor: number | null,
  target: number | null,
): { percent: number; end: number; label: number } | null {
  if (floor === null || !Number.isFinite(floor)) {
    return null;
  }
  const percent = target === null || !Number.isFinite(target) ? floor : Math.min(floor, target);
  if (percent <= 0) {
    return null;
  }
  const end = Math.min(1, percent / 100);
  return { percent, end, label: end / 2 };
}

/**
 * The level now and the car's own limit as ticks on the 0..100 % target slider, as fractions of the track: the
 * limit at the whole percent the car stops at. `null` for what is not known.
 */
export function targetTicks(now: number | null, limit: number | null): { now: number | null; limit: number | null } {
  const at = (percent: number): number => Math.min(1, Math.max(0, percent / 100));
  return {
    now: now === null || !Number.isFinite(now) ? null : at(now),
    limit: limit === null || !Number.isFinite(limit) ? null : at(chargeCeiling(limit)),
  };
}

/** A word under a slider's mark: its tick's centre and its own width, in the row's pixels. */
export interface MarkWord {
  key: string;
  center: number;
  width: number;
}

/** Where a word goes: its left edge in the row, and its line under the track (0 is the first). */
export interface PlacedMark {
  key: string;
  left: number;
  level: number;
}

/**
 * The words under a slider's marks ("min 30 %", "nu", "gräns"), as the app places them: each centred under its
 * tick and kept inside the row, taken in track order; a word that would come within `gap` of one already on a
 * line goes one line further down, so no two ever overlap.
 */
export function placeMarks(marks: readonly MarkWord[], containerWidth: number, gap: number): PlacedMark[] {
  const placed: (PlacedMark & { width: number })[] = [];
  for (const mark of [...marks].sort((a, b) => a.center - b.center)) {
    const left = Math.min(Math.max(0, mark.center - mark.width / 2), Math.max(0, containerWidth - mark.width));
    let level = 0;
    while (
      placed.some(
        (other) => other.level === level && left < other.left + other.width + gap && other.left < left + mark.width + gap,
      )
    ) {
      level += 1;
    }
    placed.push({ key: mark.key, left, level, width: mark.width });
  }
  return placed.map(({ key, left, level }) => ({ key, left, level }));
}
