// The heat card's answers (`spotnav/get_heat` and `spotnav/update_heat`, API v1), decoded strictly. The backend's
// shapes are `custom_components/spotnav/heat/view.py` and `api/heat.py`; their fixtures are `tests/fixtures/heat_card/`.
// Every named key must be present and right: a missing or mistyped one makes the whole answer unreadable, never
// half-shown. A key the card does not know is ignored (an additive backend key must not blank the card); the tests
// reject it (see `contract.ts`).

import { hasAllKeys, hasUnknownKeys } from "./contract";

export const HEAT_API_VERSION = 1;
/** `spotnav/get_heat_history`'s version (`heat-history.ts`). */
export const HEAT_HISTORY_API_VERSION = 1;

export type Spec =
  | "number"
  | "string"
  | "boolean"
  | "any"
  | { nullable: Spec }
  | { array: Spec }
  | { object: Record<string, Spec> };

export const N = (spec: Spec): Spec => ({ nullable: spec });
export const A = (spec: Spec): Spec => ({ array: spec });
export const O = (fields: Record<string, Spec>): Spec => ({ object: fields });

/** Whether `value` has the shape `spec` names (every named key present; unknown keys ignored, see `contract.ts`). */
export function fits(value: unknown, spec: Spec): boolean {
  if (spec === "any") {
    return true;
  }
  if (spec === "number") {
    return typeof value === "number" && Number.isFinite(value);
  }
  if (spec === "string" || spec === "boolean") {
    return typeof value === spec;
  }
  if ("nullable" in spec) {
    return value === null || fits(value, spec.nullable);
  }
  if ("array" in spec) {
    return Array.isArray(value) && value.every((item) => fits(item, spec.array));
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(spec.object);
  if (!hasAllKeys(record, keys) || hasUnknownKeys(record, keys)) {
    return false;
  }
  return keys.every((key) => fits(record[key], spec.object[key]!));
}

const NIGHT = O({ on: "boolean", delta_c: "number", start: "string", end: "string" });
const CONFIG = O({
  target_c: "number",
  mode: "string",
  away_until: N("string"),
  away_delta_c: "number",
  night: NIGHT,
  indoor_sensors: A("string"),
  outdoor_sensor: N("string"),
  control_path: "string",
  control_entity: N("string"),
  control_entity_b: N("string"),
  hot_water_entity: N("string"),
  zones: A(O({ entity_id: "string", offset_c: "number" })),
  indoor_offsets: A(O({ entity_id: "string", offset_c: "number" })),
  control_enabled: "boolean",
  heat_meter_entity: N("string"),
});
const CHOICE = O({ entity_id: "string", name: "string", value: N("string") });
const ZONE_CHOICE = O({ entity_id: "string", name: "string", value: N("string"), kind: "string" });
const RUN = O({ start: "string", end: "string", kwh: "number" });
const EFFECT = O({ lower_share: "number", rebound: "number", blocks: "number" });
const COMFORT = { coldest_min_c: N("number"), minutes_below: "number" } as const;
/** A day's review (`heat/review.py`): the relay's real lowering and SpotNav's plan, money and comfort. */
export const REVIEW = O({
  date: "string",
  low_c: "number",
  effect: EFFECT,
  day: N(O({ coldest_min_c: "number", minutes_below: "number" })),
  relay: O({ hours: "number", value: "number", saved_kwh: "number", measured: "number", ...COMFORT }),
  plan: O({ hours: "number", value: "number", saved_kwh: "number", overlap_hours: "number", ...COMFORT }),
});

export interface IndoorPoint {
  start: string;
  mean_c: number;
  coldest_c: number;
  /** The coldest room's sensor at the step (named in `IndoorDay.rooms`). */
  coldest: string;
}

export interface IndoorDay {
  start: string;
  end: string;
  step_min: number;
  low_c: number;
  high_c: number;
  points: IndoorPoint[];
  /** The history's setpoint per hour; `null` on the live card, whose band hours carry it. */
  setpoint: { start: string; target_c: number; action: string }[] | null;
  /** The chosen sensors' names, so the chart can name the coldest room at a step. */
  rooms: { entity_id: string; name: string }[];
}

export const INDOOR_DAY = O({
  start: "string",
  end: "string",
  step_min: "number",
  low_c: "number",
  high_c: "number",
  points: A(O({ start: "string", mean_c: "number", coldest_c: "number", coldest: "string" })),
  setpoint: N(A(O({ start: "string", target_c: "number", action: "string" }))),
  rooms: A(O({ entity_id: "string", name: "string" })),
});

/** The Boost button's state (`heat/dhw_boost.py`): `null` where the pump has no boost that ends by itself. */
export const BOOST = O({
  state: "string",
  source: N("string"),
  started: N("string"),
  ends: N("string"),
  ends_estimated: "boolean",
  stoppable: "boolean",
});
/** A hot-water level's gate to live (`heat/dhw_review.gate`). */
const GATE = O({
  level: "string",
  days: "number",
  days_needed: "number",
  weekdays: "number",
  peaks: "number",
  peaks_needed: "number",
  hits: "number",
  hit_needed: "number",
  saved: "number",
  open: "boolean",
  backed_off: N("string"),
});
/** The site's hot water (`heat/dhw_runtime.py`): peaks, the boosts planned, today's runs, the last review, the gate. */
export const HOT_WATER = O({
  boost: N(BOOST),
  peaks: A(O({ ready_by: "string", kwh: "number", learned: "boolean" })),
  planned: A(
    O({
      start: "string",
      end: "string",
      kwh: "number",
      price: "number",
      reason: "string",
      peak: "string",
      live: "boolean",
      pump_run: N("string"),
    }),
  ),
  runs_today: A(O({ start: "string", end: "string", kwh: "number", source: "string" })),
  review: N(
    O({
      date: "string",
      mode: "string",
      would_cost: "number",
      actual_cost: "number",
      saved: "number",
      moved_kwh: "number",
      hits: "number",
      peaks: "number",
      misses: "number",
      cold_risks: "number",
      blocks_cut: "number",
      a_mode: "string",
      per_peak: A(
        O({ peak: "string", hit: "boolean", pick: N("string"), pump_run: N("string"), miss: "boolean", cold_risk: "boolean" }),
      ),
    }),
  ),
  gate: GATE,
  level: N("string"),
  mode: "string",
  blocking: N(
    O({
      mode: "string",
      gate: GATE,
      cuts: A(O({ start: "string", end: "string", reason: "string", peak: N("string"), live: "boolean" })),
      tank: "boolean",
      strict: "boolean",
    }),
  ),
});
const BOOST_ANSWER = O({ api_version: "number", ok: "boolean", error: N("string"), boost: N(BOOST) });

const ANSWER = O({
  api_version: "number",
  site_id: "string",
  now: "string",
  time_zone: "string",
  writable: "boolean",
  shadow: "boolean",
  config: CONFIG,
  comfort: O({ target_c: "number", low_c: "number", high_c: "number", mode: "string", away_until: N("string"), aim_c: "number" }),
  indoor: O({
    value_c: N("number"),
    coldest: N("string"),
    coldest_name: N("string"),
    sensors: A(O({ entity_id: "string", name: "string", value_c: N("number"), rise_c: N("number"), status: "string", offset_c: "number" })),
    relative_c: N("number"),
  }),
  outdoor: N(O({ value_c: N("number"), entity_id: N("string"), name: N("string"), source: "string" })),
  zones: A(
    O({
      entity_id: "string",
      name: "string",
      kind: "string",
      offset_c: "number",
      aim_c: "number",
      current_c: N("number"),
      setpoint_c: N("number"),
      hvac_mode: N("string"),
      heating: N("boolean"),
      power_w: N("number"),
      available: "boolean",
    }),
  ),
  pump: N(
    O({
      integration: "string",
      brand: "string",
      label: "string",
      priority: N("string"),
      compressor_running: N("boolean"),
      supply_c: N("number"),
      hot_water_c: N("number"),
      degree_minutes: N("number"),
      immersion_on: N("boolean"),
      price_control: "string",
      control: "string",
      capabilities: A("string"),
      disabled: A("string"),
      device_id: N("string"),
      paths: A(O({ path: "string", control_entity: "string", control_entity_b: N("string") })),
    }),
  ),
  meter: N(
    O({
      status: "string",
      entity_id: N("string"),
      name: N("string"),
      source: N("string"),
      setting_offered: "boolean",
      candidates: A("string"),
      kw_now: N("number"),
      state: N("string"),
      baseline_kw: N("number"),
    }),
  ),
  today: N(
    O({
      date: "string",
      total_kwh: "number",
      heat_kwh: "number",
      hot_water_kwh: N("number"),
      zones_kwh: N("number"),
      runs: A(RUN),
      baseline_kw: N("number"),
      cost: N("number"),
      money: N(O({ currency: "string", major_unit: "string", minor_unit: "string" })),
    }),
  ),
  status: O({
    pump_state: N("string"),
    pump_state_source: N("string"),
    kw: N("number"),
    would: N(O({ action: "string", target_c: "number", until: N("string"), reason: "string" })),
    guard: N("string"),
  }),
  band: O({
    date: "string",
    hours: A(
      O({
        start: "string",
        action: "string",
        target_c: "number",
        reason: "string",
        price: N("number"),
        past: "boolean",
        hot_water: "boolean",
        car: "boolean",
        relay_min: N("number"),
        relay_expected: "boolean",
        executed_min: N("number"),
        raised_min: N("number"),
      }),
    ),
    hot_water_pick: N("string"),
  }),
  indoor_day: N(INDOOR_DAY),
  notes: A(O({ code: "string", params: "any" })),
  choices: N(
    O({
      indoor: A(CHOICE),
      outdoor: A(CHOICE),
      zones: A(ZONE_CHOICE),
      relay: A(CHOICE),
      offset: A(CHOICE),
      setpoint: A(CHOICE),
      hot_water: A(CHOICE),
      meter: N(A(CHOICE)),
    }),
  ),
  relay: N(
    O({
      entity_id: "string",
      name: "string",
      closed: N("boolean"),
      since: N("string"),
      source: N("string"),
      automation: N(O({ entity_id: "string", name: "string" })),
      effect: EFFECT,
    }),
  ),
  review: N(REVIEW),
  control: N(
    O({
      enabled: "boolean",
      state: "string",
      reason: N("string"),
      automation: N(O({ entity_id: "string", name: "string" })),
      check: N("string"),
      lowered: "boolean",
      since: N("string"),
      until: N("string"),
      paused_until: N("string"),
      raised: "boolean",
      raise_until: N("string"),
      path: N("string"),
      untested: "boolean",
      enable_entities: N(O({ integration: "string", entities: A(O({ entity_id: "string", name: "string" })) })),
      offset_home: N(O({ entity_id: "string", value: "number", home: "number" })),
    }),
  ),
  ahead: A(O({ start: "string", action: "string", target_c: "number", reason: "string", hot_water: "boolean" })),
  hot_water: N(HOT_WATER),
});

const WRITE = O({
  api_version: "number",
  ok: "boolean",
  error: N("string"),
  field_errors: A(O({ field: "string", code: "string" })),
  config: N(CONFIG),
});

export interface HeatNight {
  on: boolean;
  delta_c: number;
  start: string;
  end: string;
}

export interface HeatConfig {
  target_c: number;
  mode: string;
  away_until: string | null;
  /** The setback while away (°C below the target, 1 to 6 in half degrees; the plan never goes under 16 °C). */
  away_delta_c: number;
  night: HeatNight;
  indoor_sensors: string[];
  outdoor_sensor: string | null;
  control_path: string;
  control_entity: string | null;
  control_entity_b: string | null;
  hot_water_entity: string | null;
  zones: { entity_id: string; offset_c: number }[];
  /** Each indoor sensor's room aim against the house target (°C); a sensor without one is 0. */
  indoor_offsets: { entity_id: string; offset_c: number }[];
  /** SpotNav's opt-in to switch the heat pump's relay itself ("Styr värmen"). */
  control_enabled: boolean;
  heat_meter_entity: string | null;
}

export interface HeatChoice {
  entity_id: string;
  name: string;
  value: string | null;
  kind?: string;
}

export interface HeatBandHour {
  start: string;
  startMs: number;
  action: string;
  target_c: number;
  reason: string;
  price: number | null;
  past: boolean;
  hot_water: boolean;
  car: boolean;
  /** Minutes the heat pump's relay was (or is expected to be) closed in the hour; `null` without a relay. */
  relay_min: number | null;
  relay_expected: boolean;
  /** Minutes SpotNav itself held the relay closed in the hour; `null` without the heat control. */
  executed_min: number | null;
  /** Minutes SpotNav held SG Ready's second contact (B, the raise) closed; `null` where the pump cannot be raised. */
  raised_min: number | null;
}

export interface HeatAheadHour {
  start: string;
  startMs: number;
  action: string;
  target_c: number;
  reason: string;
  hot_water: boolean;
}

export interface HeatEffect {
  lower_share: number;
  rebound: number;
  blocks: number;
}

export interface HeatReviewComfort {
  coldest_min_c: number | null;
  minutes_below: number;
}

export interface HeatReview {
  date: string;
  low_c: number;
  effect: HeatEffect;
  day: { coldest_min_c: number; minutes_below: number } | null;
  relay: HeatReviewComfort & { hours: number; value: number; saved_kwh: number; measured: number };
  plan: HeatReviewComfort & { hours: number; value: number; saved_kwh: number; overlap_hours: number };
}

export interface HeatRelay {
  entity_id: string;
  name: string;
  closed: boolean | null;
  since: string | null;
  source: string | null;
  automation: { entity_id: string; name: string } | null;
  effect: HeatEffect;
}

/** The paths steered through a switch a person wires: their words name the relay. */
export function isRelayPath(path: string | null): boolean {
  return path === null || path === "none" || path === "relay" || path === "sg_ready";
}

/** SpotNav's own steering of the pump ("Styr värmen"), `heat/control.py`. */
export interface HeatControl {
  enabled: boolean;
  /** "off", "verifying", "running" or "waiting". */
  state: string;
  /** The check's phase while verifying, the rules' reason while running, what it waits for while waiting. */
  reason: string | null;
  automation: { entity_id: string; name: string } | null;
  check: string | null;
  lowered: boolean;
  since: string | null;
  until: string | null;
  paused_until: string | null;
  /** SpotNav holds SG Ready's B closed (the pump raises its heat), until when. */
  raised: boolean;
  raise_until: string | null;
  /** The control path steered ("relay", "sg_ready", "sg_ready_api", "offset"); `null` while it has nothing to steer. */
  path: string | null;
  /** The path has not run on a real heat pump yet: the card says so. */
  untested: boolean;
  /** The path's entities that are disabled in their integration: enabled on the person's confirm; `null` when none is. */
  enable_entities: { integration: string; entities: { entity_id: string; name: string }[] } | null;
  /** A stored offset that reads other than the household's own value while SpotNav holds nothing: the settings
   * show it with "put it back" and "keep" (`update_heat`'s `offset_home`); `null` when the two agree. */
  offset_home: { entity_id: string; value: number; home: number } | null;
}

export interface HeatZone {
  entity_id: string;
  name: string;
  kind: string;
  offset_c: number;
  aim_c: number;
  current_c: number | null;
  setpoint_c: number | null;
  hvac_mode: string | null;
  heating: boolean | null;
  power_w: number | null;
  available: boolean;
}

export interface HeatAnswer {
  api_version: number;
  site_id: string;
  now: string;
  time_zone: string;
  writable: boolean;
  shadow: boolean;
  config: HeatConfig;
  comfort: { target_c: number; low_c: number; high_c: number; mode: string; away_until: string | null; aim_c: number };
  indoor: {
    value_c: number | null;
    coldest: string | null;
    coldest_name: string | null;
    sensors: { entity_id: string; name: string; value_c: number | null; rise_c: number | null; status: string; offset_c: number }[];
    /** The coldest room's reading less its offset: what the comfort band is compared with. */
    relative_c: number | null;
  };
  outdoor: { value_c: number | null; entity_id: string | null; name: string | null; source: string } | null;
  zones: HeatZone[];
  pump: {
    integration: string;
    brand: string;
    label: string;
    priority: string | null;
    compressor_running: boolean | null;
    supply_c: number | null;
    hot_water_c: number | null;
    degree_minutes: number | null;
    immersion_on: boolean | null;
    price_control: string;
    control: string;
    capabilities: string[];
    disabled: string[];
    device_id: string | null;
    /** The control paths the pump's own entities offer: a chosen path's entities are filled in from them. */
    paths: { path: string; control_entity: string; control_entity_b: string | null }[];
  } | null;
  meter: {
    status: string;
    entity_id: string | null;
    name: string | null;
    source: string | null;
    setting_offered: boolean;
    candidates: string[];
    kw_now: number | null;
    state: string | null;
    baseline_kw: number | null;
  } | null;
  today: {
    date: string;
    total_kwh: number;
    heat_kwh: number;
    hot_water_kwh: number | null;
    /** The heat zones' own energy today (floor heating, an air-to-air pump), already in `heat_kwh`; `null` without. */
    zones_kwh: number | null;
    runs: { start: string; end: string; kwh: number }[];
    baseline_kw: number | null;
    cost: number | null;
    money: { currency: string; major_unit: string; minor_unit: string } | null;
  } | null;
  status: {
    pump_state: string | null;
    pump_state_source: string | null;
    kw: number | null;
    would: { action: string; target_c: number; until: string | null; reason: string } | null;
    guard: string | null;
  };
  band: { date: string; hours: HeatBandHour[]; hot_water_pick: string | null };
  /** The day's measured indoor curve up to now (`heat-day-chart.ts`); `null` without indoor sensors. */
  indoor_day: IndoorDay | null;
  notes: { code: string; params: Record<string, unknown> }[];
  choices: {
    indoor: HeatChoice[];
    outdoor: HeatChoice[];
    zones: HeatChoice[];
    relay: HeatChoice[];
    offset: HeatChoice[];
    setpoint: HeatChoice[];
    hot_water: HeatChoice[];
    meter: HeatChoice[] | null;
  } | null;
  relay: HeatRelay | null;
  review: HeatReview | null;
  control: HeatControl | null;
  /** The plan from the running hour on, twelve hours at most (the strip under the dial); it ends at midnight until
   * tomorrow has its prices. */
  ahead: HeatAheadHour[];
  /** The site's hot water; `null` without the heat shadow. */
  hot_water: HeatHotWater | null;
}

/** The Boost button (`hot_water.boost`). */
export interface HeatBoost {
  /** "idle", "starting", "running" or "failed". */
  state: string;
  /** "user" (the button's or the household's own), "plan" (SpotNav's planned boost) or "pump" (its own run). */
  source: string | null;
  started: string | null;
  ends: string | null;
  /** `ends` is the learned run length's estimate, not a timer's. */
  ends_estimated: boolean;
  /** The household's own boost that can be ended early: the button offers "stop". */
  stoppable: boolean;
}

export interface HeatHotWaterPlanned {
  start: string;
  end: string;
  kwh: number;
  /** The run's mean price (minor unit per kWh): the sell price on sun. */
  price: number;
  /** "cheap" or "sun". */
  reason: string;
  /** The peak it fills ("06:00", local). */
  peak: string;
  /** Written to the pump (live); else what SpotNav would do (shadow). */
  live: boolean;
  /** The pump's own run the boost would have replaced, once seen. */
  pump_run: string | null;
}

export interface HeatHotWaterReview {
  date: string;
  mode: string;
  would_cost: number;
  actual_cost: number;
  saved: number;
  moved_kwh: number;
  hits: number;
  peaks: number;
  misses: number;
  /** Peaks a block held the pump's run into (level A). */
  cold_risks: number;
  /** Hours level A took out of the heat plan that day. */
  blocks_cut: number;
  /** Level A's mode that day: "shadow" or "live". */
  a_mode: string;
  per_peak: { peak: string; hit: boolean; pick: string | null; pump_run: string | null; miss: boolean; cold_risk: boolean }[];
}

export interface HeatHotWaterGate {
  level: string;
  days: number;
  days_needed: number;
  weekdays: number;
  peaks: number;
  peaks_needed: number;
  hits: number;
  hit_needed: number;
  saved: number;
  open: boolean;
  backed_off: string | null;
}

/** The site's hot water: the button, the peaks, the boosts planned, today's runs, the last review and the gate. */
export interface HeatHotWater {
  boost: HeatBoost | null;
  peaks: { ready_by: string; kwh: number; learned: boolean }[];
  planned: HeatHotWaterPlanned[];
  runs_today: { start: string; end: string; kwh: number; source: string }[];
  review: HeatHotWaterReview | null;
  gate: HeatHotWaterGate;
  /** "B" (a boost route), "A" (a block path only) or `null`. */
  level: string | null;
  /** "shadow", "live" or "watch". */
  mode: string;
  /** Level A, where the path blocks hot water too; `null` elsewhere. */
  blocking: HeatHotWaterBlocking | null;
}

/** An hour level A takes out of the heat plan (`heat/dhw_level_a.py`). */
export interface HeatBlockingCut {
  start: string;
  end: string;
  /** "peak" (before the shower at `peak`), "day_cap" (the day's blocked time), "hygiene" (the periodic increase) or
   * "moved" (the plan chose again around a kept hour). */
  reason: string;
  peak: string | null;
  /** Out of the plan steered by; else what SpotNav would do (shadow). */
  live: boolean;
}

/** Level A: the heat plan's blocks kept clear of the hot water. */
export interface HeatHotWaterBlocking {
  /** "shadow", "live" or "watch". */
  mode: string;
  gate: HeatHotWaterGate;
  cuts: HeatBlockingCut[];
  /** A tank sensor reads: the tank rule stands in for the day's cap. */
  tank: boolean;
  /** After a back-off: the stricter rules hold (a wider guard, a lower cap) until a clean week. */
  strict: boolean;
}

export interface HeatBoostAnswer {
  api_version: number;
  ok: boolean;
  error: string | null;
  boost: HeatBoost | null;
}

export interface HeatWriteAnswer {
  api_version: number;
  ok: boolean;
  error: string | null;
  field_errors: { field: string; code: string }[];
  config: HeatConfig | null;
}

export type Decoded<T> = { ok: true; value: T } | { ok: false; failure: "unsupported" | "malformed" };

function versionOf(raw: unknown): unknown {
  return typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>)["api_version"] : undefined;
}

function instant(text: string): number | null {
  const ms = Date.parse(text);
  return Number.isFinite(ms) ? ms : null;
}

/** `spotnav/get_heat`'s answer, or why it cannot be read. */
export function decodeHeat(raw: unknown): Decoded<HeatAnswer> {
  if (versionOf(raw) !== HEAT_API_VERSION) {
    return { ok: false, failure: typeof versionOf(raw) === "number" ? "unsupported" : "malformed" };
  }
  if (!fits(raw, ANSWER)) {
    return { ok: false, failure: "malformed" };
  }
  const answer = raw as HeatAnswer;
  const notesFit = answer.notes.every((note) => typeof note.params === "object" && note.params !== null && !Array.isArray(note.params));
  if (!notesFit || instant(answer.now) === null) {
    return { ok: false, failure: "malformed" };
  }
  const hours: HeatBandHour[] = [];
  for (const hour of answer.band.hours) {
    const ms = instant(hour.start);
    if (ms === null) {
      return { ok: false, failure: "malformed" };
    }
    hours.push({ ...hour, startMs: ms });
  }
  const ahead: HeatAheadHour[] = [];
  for (const hour of answer.ahead) {
    const ms = instant(hour.start);
    if (ms === null) {
      return { ok: false, failure: "malformed" };
    }
    ahead.push({ ...hour, startMs: ms });
  }
  return { ok: true, value: { ...answer, band: { ...answer.band, hours }, ahead } };
}

/** `spotnav/update_heat`'s answer, or why it cannot be read. */
export function decodeHeatWrite(raw: unknown): Decoded<HeatWriteAnswer> {
  if (versionOf(raw) !== HEAT_API_VERSION) {
    return { ok: false, failure: typeof versionOf(raw) === "number" ? "unsupported" : "malformed" };
  }
  return fits(raw, WRITE) ? { ok: true, value: raw as HeatWriteAnswer } : { ok: false, failure: "malformed" };
}

/** `spotnav/heat_boost`'s answer, or why it cannot be read. */
export function decodeHeatBoost(raw: unknown): Decoded<HeatBoostAnswer> {
  if (versionOf(raw) !== HEAT_API_VERSION) {
    return { ok: false, failure: typeof versionOf(raw) === "number" ? "unsupported" : "malformed" };
  }
  return fits(raw, BOOST_ANSWER) ? { ok: true, value: raw as HeatBoostAnswer } : { ok: false, failure: "malformed" };
}

/** A room's name without the sensor's trailing "temperature" word ("Ovanvåning temperatur" → "Ovanvåning"). */
export function roomName(name: string): string {
  const trimmed = name.replace(/\s+(temperatur|temperature|température|temperatura|lämpötila|temperatuur)$/iu, "").trim();
  return trimmed === "" ? name : trimmed;
}
