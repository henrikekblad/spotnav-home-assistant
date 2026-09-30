// Energy a target state of charge needs, mirroring the backend's `soc_estimate.target_need_kwh`:
// the target is rounded to a whole percent (Python `round`, halves to even), capped at the
// vehicle's limit (`floor(max)`, at most 100), and the energy is
//
//     (min(target, ceiling) - soc) x capacity / efficiency
//
// Zero at or below the current level, unknown without a battery size. The dashboard's `soc`
// block states `efficiency` and `vehicle_max_percent`, so a moving slider needs no backend call;
// `test/target-need.test.ts` pins the result against the committed fixtures.

/** Python's `round()`: halves go to the even neighbour (80.5 is 80, 81.5 is 82). */
export function pythonRound(value: number): number {
  const floor = Math.floor(value);
  const diff = value - floor;
  if (diff < 0.5) {
    return floor;
  }
  if (diff > 0.5) {
    return floor + 1;
  }
  return floor % 2 === 0 ? floor : floor + 1;
}

export function chargeCeiling(maxPercent: number | null): number {
  if (maxPercent === null || !Number.isFinite(maxPercent)) {
    return 100;
  }
  return Math.min(100, Math.max(0, Math.floor(maxPercent)));
}

export interface NeedFacts {
  soc: number | null;
  capacityKwh: number | null;
  targetPercent: number;
  maxPercent: number | null;
  efficiency: number;
}

export function effectiveTarget(targetPercent: number, maxPercent: number | null): number {
  return Math.min(pythonRound(targetPercent), chargeCeiling(maxPercent));
}

/** Wall energy the target needs in kWh, or `null` when unknown. */
export function targetNeedKwh(facts: NeedFacts): number | null {
  const { soc, capacityKwh } = facts;
  if (soc === null || capacityKwh === null || !(capacityKwh > 0) || !Number.isFinite(facts.targetPercent)) {
    return null;
  }
  const needed = Math.max(0, ((effectiveTarget(facts.targetPercent, facts.maxPercent) - soc) / 100) * capacityKwh);
  return needed === 0 ? 0 : needed / facts.efficiency;
}

export function pythonRoundedAbove(targetPercent: number, maxPercent: number | null): boolean {
  return maxPercent !== null && pythonRound(targetPercent) > chargeCeiling(maxPercent);
}
