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
  zones: A(O({ entity_id: "string", offset_c: "number", control: "boolean" })),
  indoor_offsets: A(O({ entity_id: "string", offset_c: "number" })),
  control_enabled: "boolean",
  water_heater: N(O({ entity_id: "string", control: "boolean", tank_sensor: N("string") })),
  pool: N(
    O({
      heat_pump: N(O({ entity_id: "string", shape: "string", switch_id: N("string"), number_id: N("string") })),
      circulation_id: N("string"),
      water_sensor: N("string"),
      target_c: "number",
      season_open: "boolean",
      filter_hours: N("number"),
      heat_pump_kw: N("number"),
      circulation_kw: N("number"),
      control_heat_pump: "boolean",
      control_circulation: "boolean",
    }),
  ),
  spa: N(
    O({
      entity_id: "string",
      family: "string",
      bath_c: "number",
      idle_c: "number",
      floor_c: "number",
      baths: A(O({ weekdays: A("number"), time: "string" })),
      bath_once: N("string"),
      heater_kw: N("number"),
      control: "boolean",
    }),
  ),
  sauna: N(
    O({
      entity_id: "string",
      family: "string",
      target_c: "number",
      sessions: A(O({ weekdays: A("number"), time: "string" })),
      session_once: N("string"),
      heater_kw: N("number"),
      lock_id: N("string"),
      safety_declared: "boolean",
      control: "boolean",
    }),
  ),
  heat_notifications: O({ targets: A("string"), events: A("string") }),
  heat_meter_entity: N("string"),
});
const CHOICE = O({ entity_id: "string", name: "string", value: N("string") });
const ZONE_CHOICE = O({ entity_id: "string", name: "string", value: N("string"), kind: "string" });
/** A detected candidate: how sure the detection is (`ask`: the settings ask the person once). */
const RANKED_CHOICE = O({ entity_id: "string", name: "string", value: N("string"), score: "number", ask: "boolean" });
const POOL_PUMP_CHOICE = O({
  entity_id: "string",
  name: "string",
  value: N("string"),
  score: "number",
  ask: "boolean",
  shape: "string",
  switch_id: N("string"),
  number_id: N("string"),
  cloud: "boolean",
});
const SAUNA_CHOICE = O({
  entity_id: "string",
  name: "string",
  value: N("string"),
  score: "number",
  ask: "boolean",
  family: "string",
  lock_id: N("string"),
  cloud: "boolean",
});
const SPA_CHOICE = O({
  entity_id: "string",
  name: "string",
  value: N("string"),
  score: "number",
  ask: "boolean",
  family: "string",
  controllable: "boolean",
  cloud: "boolean",
});
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
/** SpotNav setting a zone's thermostat (`heat/zones_control.py`); `null` for a zone watched only. */
const ZONE_CONTROL = O({
  enabled: "boolean",
  state: "string",
  reason: N("string"),
  home_c: N("number"),
  held_c: N("number"),
  until: N("string"),
  check: N("string"),
  action: N("string"),
  automation: N(O({ entity_id: "string", name: "string" })),
  paused_until: N("string"),
});
/** A plain water heater on a switch (`heat/water_heater_control.py`). */
export const WATER_HEATER = O({
  entity_id: "string",
  name: "string",
  on: N("boolean"),
  metered: "boolean",
  meter_waiting: "boolean",
  mode: "string",
  kw: N("number"),
  state: "string",
  reason: N("string"),
  check: N("string"),
  last_full: N("string"),
  until: N("string"),
  windows: A(O({ start: "string", end: "string", reason: "string" })),
  live: "boolean",
  automation: N(O({ entity_id: "string", name: "string" })),
  paused_until: N("string"),
});
const POOL_PART = { live: "boolean", state: "string", reason: N("string") } as const;
/** The pool in shadow (`get_heat`'s `pool`, `pool/runtime.py`). */
const POOL = O({
  season_open: "boolean",
  water_c: N("number"),
  water_source: N("string"),
  target_c: "number",
  heat_pump: N(O({ on: N("boolean"), heating: N("boolean"), kw: N("number"), ...POOL_PART })),
  circulation: N(O({ on: N("boolean"), kw: N("number"), ...POOL_PART })),
  today: O({
    need_h: N("number"),
    need_source: N("string"),
    done_h: "number",
    planned_h: "number",
    heat_h: N("number"),
    kwh: N("number"),
  }),
  planned: A(O({ start: "string", end: "string", what: "string", reason: "string" })),
  extra: N(O({ hours: "number", until: "string" })),
  season_hint: "boolean",
  would: N(O({ action: "string", until: N("string"), reason: "string" })),
  notes: A(O({ code: "string", params: "any" })),
});
const SESSION = O({ weekdays: A("number"), time: "string" });
const NOT_READY = O({
  at: "string",
  cause: "string",
  reach_c: "number",
  short_c: "number",
  short_kwh: "number",
  later: N("string"),
});
/** What the spa's and the sauna's status share (`spa/runtime.py`). */
const DEADLINE_COMMON = {
  family: "string",
  heating: N("boolean"),
  kw: N("number"),
  rate_c_h: "number",
  rate_learned: "boolean",
  state: N("string"),
  ready_at: N("string"),
  live: "boolean",
  control: O({ state: "string", reason: N("string"), paused_until: N("string") }),
  would: N(O({ action: "string", until: N("string"), reason: "string" })),
  not_ready: N(NOT_READY),
  planned: A(O({ start: "string", end: "string", what: "string", reason: "string" })),
  notes: A(O({ code: "string", params: "any" })),
} as const;
const SPA = O({
  ...DEADLINE_COMMON,
  water_c: N("number"),
  bath_c: "number",
  idle_c: "number",
  floor_c: "number",
  next_bath: N(O({ at: "string", source: "string" })),
  baths: A(SESSION),
  bath_once: N("string"),
  bath_now: N(O({ until: "string" })),
});
const SAUNA = O({
  ...DEADLINE_COMMON,
  temp_c: N("number"),
  target_c: "number",
  next_session: N(O({ at: "string", source: "string" })),
  sessions: A(SESSION),
  session_once: N("string"),
  session_now: N(O({ until: "string" })),
  locked: N("boolean"),
  lock_id: N("string"),
  safety_declared: N("boolean"),
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
      control: N(ZONE_CONTROL),
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
      water_heater: A(CHOICE),
      tank_sensor: A(CHOICE),
      pool: N(
        O({
          heat_pump: A(POOL_PUMP_CHOICE),
          circulation: A(RANKED_CHOICE),
          water_sensor: A(RANKED_CHOICE),
          suggested: N(
            O({
              heat_pump: O({ entity_id: "string", shape: "string", switch_id: N("string"), number_id: N("string") }),
              circulation_id: N("string"),
              water_sensor: N("string"),
              ask: "boolean",
            }),
          ),
        }),
      ),
      spa: A(SPA_CHOICE),
      sauna: A(SAUNA_CHOICE),
      phones: A(O({ service: "string", name: "string" })),
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
  water_heater: N(WATER_HEATER),
  pool: N(POOL),
  spa: N(SPA),
  sauna: N(SAUNA),
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
  /** `control`: the person's opt-in that lets SpotNav set the thermostat (relative to its own setpoint). */
  zones: { entity_id: string; offset_c: number; control: boolean }[];
  /** Each indoor sensor's room aim against the house target (°C); a sensor without one is 0. */
  indoor_offsets: { entity_id: string; offset_c: number }[];
  /** SpotNav's opt-in to switch the heat pump's relay itself ("Styr värmen"). */
  control_enabled: boolean;
  /** A plain water heater's switch, the opt-in to let SpotNav switch it, and an optional tank sensor. */
  water_heater: HeatWaterHeaterConfig | null;
  /** The pool: its heat pump, circulation pump and water sensor, the wanted temperature and the opt-ins. */
  pool: HeatPoolConfig | null;
  /** The spa: its bath and idle temperatures, the bath times and the opt-in. */
  spa: HeatSpaConfig | null;
  /** The sauna: its temperature, the session times, its lock and the opt-in (a relay's only with its safety declared). */
  sauna: HeatSaunaConfig | null;
  /** The heat card's phones and the events they hear (`plan_at_risk`, `pool_season`). */
  heat_notifications: { targets: string[]; events: string[] };
  heat_meter_entity: string | null;
}

export interface HeatPoolHeatPump {
  entity_id: string;
  /** "climate" or "switch_number" (core Tuya's pool heat pump: a switch and a temperature number). */
  shape: string;
  switch_id: string | null;
  number_id: string | null;
}

export interface HeatPoolConfig {
  heat_pump: HeatPoolHeatPump | null;
  circulation_id: string | null;
  water_sensor: string | null;
  target_c: number;
  season_open: boolean;
  /** `null`: the filtering hours follow the water temperature. */
  filter_hours: number | null;
  heat_pump_kw: number | null;
  circulation_kw: number | null;
  control_heat_pump: boolean;
  control_circulation: boolean;
}

export interface HeatSpaConfig {
  entity_id: string;
  family: string;
  bath_c: number;
  idle_c: number;
  floor_c: number;
  /** Weekly bath times: weekdays (Monday 0) and the local time. */
  baths: { weekdays: number[]; time: string }[];
  bath_once: string | null;
  heater_kw: number | null;
  control: boolean;
}

export interface HeatSaunaConfig {
  entity_id: string;
  family: string;
  target_c: number;
  sessions: { weekdays: number[]; time: string }[];
  session_once: string | null;
  heater_kw: number | null;
  lock_id: string | null;
  safety_declared: boolean;
  control: boolean;
}

/** A detected candidate in the settings (`ask`: below 0.9, the person confirms it once). */
export interface HeatRankedChoice extends HeatChoice {
  score: number;
  ask: boolean;
}

export interface HeatPoolChoices {
  heat_pump: (HeatRankedChoice & { shape: string; switch_id: string | null; number_id: string | null; cloud: boolean })[];
  circulation: HeatRankedChoice[];
  water_sensor: HeatRankedChoice[];
  /** The one-time grouping the pool block is filled from; `null` once a pool is saved. */
  suggested: { heat_pump: HeatPoolHeatPump; circulation_id: string | null; water_sensor: string | null; ask: boolean } | null;
}

export interface HeatWaterHeaterConfig {
  entity_id: string;
  control: boolean;
  tank_sensor: string | null;
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

/** Whether anything heat-related was found: a heat pump, a meter, climate zones, or settings already made. The
 * heat card shows one line in place of its body without it; the settings popover collapses its heat section. */
export function heatFound(answer: HeatAnswer): boolean {
  return (
    answer.pump !== null ||
    answer.meter?.entity_id != null ||
    answer.zones.length > 0 ||
    (answer.choices?.zones.length ?? 0) > 0 ||
    answer.config.control_path !== "none" ||
    answer.config.indoor_sensors.length > 0 ||
    answer.config.water_heater !== null ||
    answer.config.heat_meter_entity !== null
  );
}

/** The control paths SpotNav steers itself ("Styr värmen" has a row on them). */
export const STEERED_PATHS: readonly string[] = ["relay", "sg_ready", "sg_ready_api", "offset"];

/** Whether SpotNav can steer this heating: a steered path, or control already on. Without it the comfort settings
 * (target, Home/Away, night, away, offsets) can never act, and the card and settings leave them out. */
export function heatSteers(config: { control_path: string; control_enabled: boolean }): boolean {
  return STEERED_PATHS.includes(config.control_path) || config.control_enabled;
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
  /** SpotNav setting the thermostat; `null` while the zone is watched only. */
  control: HeatZoneControl | null;
}

/** SpotNav setting a zone's thermostat, relative to the person's own setpoint (`home_c`). */
export interface HeatZoneControl {
  enabled: boolean;
  /** "off", "verifying", "running" or "waiting". */
  state: string;
  reason: string | null;
  home_c: number | null;
  /** What SpotNav holds on the thermostat now; `null`: the person's own value stands. */
  held_c: number | null;
  until: string | null;
  check: string | null;
  /** The plan's action it holds for: "lower", "preheat", "return", "night" or "away". */
  action: string | null;
  automation: { entity_id: string; name: string } | null;
  paused_until: string | null;
}

/** A plain water heater on a switch, watched or switched by SpotNav. */
export interface HeatWaterHeater {
  entity_id: string;
  name: string;
  on: boolean | null;
  metered: boolean;
  /** A power meter is there but has not yet been seen at the element's draw (500 W): treated as unmetered meanwhile. */
  meter_waiting: boolean;
  /** How full heat is seen: "metered", "tank" or "unmetered" (a fixed night window). */
  mode: string;
  kw: number | null;
  state: string;
  reason: string | null;
  check: string | null;
  last_full: string | null;
  until: string | null;
  /** The hours SpotNav allows it (or would, watched only): "cheap", "full_heat" or "max_off". */
  windows: { start: string; end: string; reason: string }[];
  /** SpotNav switches it now. */
  live: boolean;
  automation: { entity_id: string; name: string } | null;
  paused_until: string | null;
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
  band: { date: string; hours: HeatBandHour[] };
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
    water_heater: HeatChoice[];
    tank_sensor: HeatChoice[];
    pool: HeatPoolChoices | null;
    spa: (HeatRankedChoice & { family: string; controllable: boolean; cloud: boolean })[];
    sauna: (HeatRankedChoice & { family: string; lock_id: string | null; cloud: boolean })[];
    phones: { service: string; name: string }[];
  } | null;
  relay: HeatRelay | null;
  review: HeatReview | null;
  control: HeatControl | null;
  /** The plan from the running hour on, 24 hours at most, rolling across midnight (the strip under the dial); an hour of tomorrow
   * without a price is planned normal. */
  ahead: HeatAheadHour[];
  /** The site's hot water; `null` without the heat shadow. */
  hot_water: HeatHotWater | null;
  /** A plain water heater on a switch; `null` without one. */
  water_heater: HeatWaterHeater | null;
  /** The pool in shadow; `null` without one. */
  pool: HeatPool | null;
  /** The spa and the sauna in shadow; `null` without one. */
  spa: HeatSpa | null;
  sauna: HeatSauna | null;
}

/** A session that will not be ready: why, what it reaches and the earliest later hour that fits. */
export interface HeatNotReady {
  at: string;
  /** "rate", "import_target", "power_level" or "fuse". */
  cause: string;
  reach_c: number;
  short_c: number;
  short_kwh: number;
  later: string | null;
}

interface HeatDeadlineCommon {
  family: string;
  heating: boolean | null;
  kw: number | null;
  rate_c_h: number;
  rate_learned: boolean;
  /** "idle", "heating", "ready" or "late". */
  state: string | null;
  ready_at: string | null;
  live: boolean;
  control: { state: string; reason: string | null; paused_until: string | null };
  would: { action: string; until: string | null; reason: string } | null;
  not_ready: HeatNotReady | null;
  /** The heat-up ("heat", by "sun", "cheap", "bath", "now" or "floor") and the hold at the target ("hold") from now on. */
  planned: { start: string; end: string; what: string; reason: string }[];
  notes: { code: string; params: Record<string, unknown> }[];
}

export interface HeatSpa extends HeatDeadlineCommon {
  water_c: number | null;
  bath_c: number;
  idle_c: number;
  floor_c: number;
  next_bath: { at: string; source: string } | null;
  baths: { weekdays: number[]; time: string }[];
  bath_once: string | null;
  bath_now: { until: string } | null;
}

export interface HeatSauna extends HeatDeadlineCommon {
  temp_c: number | null;
  target_c: number;
  next_session: { at: string; source: string } | null;
  sessions: { weekdays: number[]; time: string }[];
  session_once: string | null;
  session_now: { until: string } | null;
  /** The door or safety lock reads locked; `null` where nothing reads. */
  locked: boolean | null;
  lock_id: string | null;
  /** A relay's declared safety measure; `null` for an integration's sauna. */
  safety_declared: boolean | null;
}

export interface HeatPoolPart {
  on: boolean | null;
  kw: number | null;
  /** SpotNav steers it (never yet: watched). */
  live: boolean;
  /** "watched" or "season_closed". */
  state: string;
  reason: string | null;
}

export interface HeatPool {
  season_open: boolean;
  water_c: number | null;
  /** "sensor" or "heat_pump" (its own reading once it has run), `null` while none reads. */
  water_source: string | null;
  target_c: number;
  heat_pump: (HeatPoolPart & { heating: boolean | null }) | null;
  circulation: HeatPoolPart | null;
  today: {
    need_h: number | null;
    /** "water", "override" or "default". */
    need_source: string | null;
    done_h: number;
    planned_h: number;
    heat_h: number | null;
    kwh: number | null;
  };
  /** From now on: "filter", "heat" or "extra", with "sun", "cheap", "need" or "extra". */
  planned: { start: string; end: string; what: string; reason: string }[];
  extra: { hours: number; until: string } | null;
  season_hint: boolean;
  would: { action: string; until: string | null; reason: string } | null;
  notes: { code: string; params: Record<string, unknown> }[];
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
