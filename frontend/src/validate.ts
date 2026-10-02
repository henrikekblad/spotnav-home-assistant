// Runtime decoder for the `spotnav/get_dashboard` API v1.
//
// A WebSocket answer is untrusted: this module turns `unknown` into typed, immutable values (parsed
// instants included) so the model and chart read fields without assertions.
//
// What the card depends on must be present and right. The top level is exact-key (an extra field means
// the document is not fully known); the price sections stay additive-tolerant. A wrong `api_version`
// is `unsupported`; anything else that does not fit is `malformed`. Neither failure carries payload
// text or an exception message.

import { localDayKey } from "./chart";
import { decodeSettingsRecord } from "./settings";
import {
  ACTION_PAUSE,
  ACTION_RESUME,
  ACTION_START,
  ACTION_STOP,
  API_VERSION,
  CHARGE_PROGRESS_STATES,
  type ChargeProgressState,
  type ChargeProgress,
  type SettingsRecord,
} from "./types";

export interface ChargerCapabilities {
  auto_price: boolean;
  current_limit: boolean;
  set_current: boolean;
  regulated_current: boolean;
  target_soc: boolean;
  load_balancing: boolean;
  refresh_vehicle: boolean;
  set_charge_limit: boolean;
  target_stop: boolean;
}

export interface DashboardCharger {
  charger_id: string;
  charger_name: string | null;
  available: boolean;
  capabilities: ChargerCapabilities;
}

export interface FiscalComponent {
  policy: string;
  effective: number | null;
  unit: string | null;
}

export interface Fiscal {
  vat: FiscalComponent;
  tax: FiscalComponent;
  transfer: FiscalComponent;
}

export interface Planning {
  state: string | null;
  reason: string | null;
  settings_revision: number | null;
  price_state: string | null;
  execution_state: string | null;
  execution_reason: string | null;
  execution_paused: boolean | null;
  applied: boolean | null;
  missing: string[] | null;
  today: string | null;
  tomorrow: string | null;
  price_wait: string | null;
  publication_at: string | null;
  must_buy_now_kwh: number | null;
}

export interface DashboardMarket {
  catalogue_state: string | null;
  area_id: string | null;
  area_name: string | null;
  countries: string[] | null;
  timezone: string | null;
  currency: string | null;
  major_unit: string | null;
  minor_unit: string | null;
  suggested_vat_percent: number | null;
}

export interface PriceInterval {
  start: string;
  startMs: number;
  end: string;
  endMs: number;
  day: string;
  duration_minutes: number;
  raw_price: number | null;
  effective_price: number | null;
  proposal_planned: boolean;
  installed_planned: boolean;
}

export interface Prices {
  state: string | null;
  reason: string | null;
  waiting_for_tomorrow: boolean | null;
  unpriced: boolean | null;
  interval_count: number;
  priced_slots: number | null;
  unpriced_slots: number | null;
  resolution_minutes: number | null;
  resolutions_minutes: number[];
  intervals: PriceInterval[];
}

export interface Period {
  start: string;
  startMs: number;
  end: string;
  endMs: number;
}

export interface Proposal {
  identity: string | null;
  settings_revision: number | null;
  periods: Period[];
  amps: number | null;
  phases: number | null;
  unpriced: boolean;
  unpriced_slots: number | null;
  priced_slots: number | null;
  planned_kwh: number | null;
  requested_kwh: number | null;
  cost: { value: number; currency: string | null } | null;
  distance_mil: number | null;
  power_kw: number | null;
}

export interface Installed {
  identity: string | null;
  periods: Period[];
  amps: number | null;
  phases: number | null;
  power_kw: number | null;
  active_period_index: number | null;
}

export interface Relation {
  applied: boolean | null;
  applied_identity: string | null;
  pending_identity: string | null;
}

export interface Plan {
  proposal: Proposal | null;
  installed: Installed | null;
  relation: Relation;
  delivered_kwh: number | null;
  remaining_kwh: number | null;
}

export interface Live {
  charging: boolean | null;
  schedule_active: boolean | null;
  requested_current_a: number | null;
  setpoint_current_a: number | null;
  measured_current_a: number | null;
}

/**
 * /**
 * The `strategy_state`: what the selected strategy is doing now, in that strategy's own shape; `null`
 * for `cheapest` and for a charger with no settings record.
 *
 * `state` and `reason` are read as free text: a value the card does not recognise is still a real
 * observation, and `model.ts` falls back to a generic sentence (never the raw code) for it.
 *
 */
export interface SolarStrategyState {
  kind: "solar";
  state: string;
  reason: string | null;
  available_w: number | null;
  requested_a: number | null;
  priority_effective: string | null;
}

export interface HybridStrategyState {
  kind: "hybrid";
  state: string;
  reason: string | null;
  grid_kwh: number | null;
  credit_kwh: number | null;
  slack_kwh: number | null;
  plan_window_active: boolean;
  forecast_configured: boolean;
}

export type StrategyState = SolarStrategyState | HybridStrategyState;

export interface SolarForecastChoice {
  id: string;
  title: string;
}

/**
 * The `site`: site-wide facts and choices behind `spotnav/update_site_settings`, or `null` without a
 * resolved site. `writable` is whether *this connection* may call that command; it is not derived
 * from `control.can_act`.
 */
export interface Site {
  name: string;
  charger_count: number;
  solar_priority: "car_first" | "battery_first";
  solar_forecast: {
    selected: string[];
    choices: SolarForecastChoice[];
  };
  active_control: {
    available: boolean;
    enabled: boolean;
    reason: string | null;
    writable: boolean;
  };
  writable: boolean;
}

/**
 * The dashboard as this card reads it: one version, every section always present (`null` or not).
 *
 * `charge_progress` and `current_range` are read independently: an unreadable block only hides that
 * feature. `soc` is decoded strictly: a document that misstates the vehicle's charge is refused whole.
 */
export interface Dashboard {
  api_version: 1;
  generated_at: string | null;
  charger: DashboardCharger;
  settings: SettingsRecord | null;
  fiscal: Fiscal | null;
  planning: Planning | null;
  market: DashboardMarket | null;
  prices: Prices;
  plan: Plan;
  live: Live;
  strategy: Strategy;
  strategy_options: string[];
  strategy_state: StrategyState | null;
  control: Control;
  charge_progress: ChargeProgress | null;
  site: Site | null;
  current_range: ChargerCurrentRange | null;
  soc: Soc | null;
  vehicles: Vehicle[];
  target_vehicle_id: string | null;
  detected_phases: number | null;
  phase_detection: PhaseDetection;
  chargers: Array<{ id: string; name: string }>;
  status: Status;
}

export interface PhaseDetection {
  source: string;
  confidence: string;
}

export interface Vehicle {
  id: string;
  name: string | null;
  soc_entity_id: string | null;
  capacity_kwh: number | null;
  capacity_source: "reported" | "stored" | null;
  consumption_kwh_per_10km: number | null;
  max_percent: number | null;
  soc_percent: number | null;
}

export interface Soc {
  value: number | null;
  age_s: number | null;
  source: "charger" | "vehicle" | null;
  estimated: boolean;
  target_percent: number | null;
  need_kwh: number | null;
  capacity_kwh: number | null;
  vehicle_name: string | null;
  vehicle_id: string | null;
  vehicles: Array<{ id: string; name: string }>;
  vehicle_max_percent: number | null;
  efficiency: number;
  missing: Array<"capacity" | "soc" | "vehicle">;
}

export interface ChargerCurrentRange {
  min_a: number;
  max_a: number;
  source: string;
}

export type DecodeFailure = "unsupported" | "malformed";

/**
 * The persisted pause record: three facts that must agree. Nothing is derived from the clock; whether
 * a pause is in force is the backend's control fact, not this object's expiry (an elapsed but
 * uncleared pause must stay showable).
 */
export interface Pause {
  choice: string | null;
  admitted_at: string | null;
  admitted_at_ms: number | null;
  expires_at: string | null;
  expires_at_ms: number | null;
}

export interface StrategyRow {
  strategy: string;
  available: boolean;
  reason: string | null;
}

export interface Strategy {
  selected: string | null;
  rows: StrategyRow[];
}

/**
 * What the backend allows right now, on two independent axes; the card renders this and derives
 * nothing.
 *
 * `immediate` is a command to the charger (Start now / Stop); `automatic` is Auto's own execution
 * (pause / resume). `pause_choices` belongs only to the automatic `pause`. A single folded action
 * cannot express both, and deriving either from `live.charging`, plan rows or pause instants would
 * be a second rule disagreeing with the execution boundary.
 */
export interface Control {
  immediate_action: string;
  immediate_action_reason: string | null;
  automatic_action: string;
  automatic_action_reason: string | null;
  pause_choices: string[];
  pause: Pause | null;
  pause_blocks_execution: boolean;
  execution_error: string | null;
  can_act: boolean;
}

export type DecodeResult =
  | { ok: true; value: Dashboard }
  | { ok: false; failure: DecodeFailure };

class Malformed extends Error {}

/**
 * Whether the serializer emitted this key at all. An explicit `null` is a stated fact; an absent key
 * means the payload is not this contract, so every field the card reads must be present. Unknown
 * extra keys stay allowed (additive v1 fields).
 */
function has(source: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(source, key);
}

function required(source: Record<string, unknown>, key: string): unknown {
  return has(source, key) ? source[key] : bad();
}

/** An ISO-8601 instant must state its offset; a naive timestamp describes no instant. */
const OFFSET_SUFFIX = /(?:Z|[+-]\d{2}:?\d{2})$/;

export const DURATION_TOLERANCE_MS = 1_000;

function bad(): never {
  throw new Malformed("malformed");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : bad();
}

function text(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" ? value : bad();
}

function textOrNull(source: Record<string, unknown>, key: string): string | null {
  const value = required(source, key);
  if (value === null) {
    return null;
  }
  return typeof value === "string" ? value : bad();
}

function numberValue(source: Record<string, unknown>, key: string): number {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : bad();
}

function numberOrNull(source: Record<string, unknown>, key: string): number | null {
  const value = required(source, key);
  if (value === null) {
    return null;
  }
  return typeof value === "number" && Number.isFinite(value) ? value : bad();
}

function booleanValue(source: Record<string, unknown>, key: string): boolean {
  const value = source[key];
  return typeof value === "boolean" ? value : bad();
}

function booleanOrNull(source: Record<string, unknown>, key: string): boolean | null {
  const value = required(source, key);
  if (value === null) {
    return null;
  }
  return typeof value === "boolean" ? value : bad();
}

function arrayValue(source: Record<string, unknown>, key: string): unknown[] {
  const value = source[key];
  return Array.isArray(value) ? value : bad();
}

function stringListOrNull(source: Record<string, unknown>, key: string): string[] | null {
  const value = required(source, key);
  if (value === null) {
    return null;
  }
  if (!Array.isArray(value)) {
    return bad();
  }
  return value.map((entry) => (typeof entry === "string" ? entry : bad()));
}

function sectionOrNull<T>(
  source: Record<string, unknown>,
  key: string,
  decode: (value: Record<string, unknown>) => T,
): T | null {
  const value = required(source, key);
  return value === null ? null : decode(record(value));
}

function numberList(source: Record<string, unknown>, key: string): number[] {
  return arrayValue(source, key).map((entry) =>
    typeof entry === "number" && Number.isFinite(entry) ? entry : bad(),
  );
}

function instantMs(value: unknown): number {
  if (typeof value !== "string" || !OFFSET_SUFFIX.test(value)) {
    return bad();
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : bad();
}

function instant(source: Record<string, unknown>, key: string): { text: string; ms: number } {
  const value = source[key];
  const ms = instantMs(value);
  return { text: value as string, ms };
}

export function isValidTimeZone(timeZone: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone });
    return true;
  } catch {
    return false;
  }
}

/**
 * Exact-key guard: every key present, no key outside the contract, so what the card shows and what
 * the contract states stay the same set.
 */
function exactKeys(source: Record<string, unknown>, keys: readonly string[]): void {
  presentKeys(source, keys);
  for (const key of Object.keys(source)) {
    if (!keys.includes(key)) {
      bad();
    }
  }
}

function presentKeys(source: Record<string, unknown>, keys: readonly string[]): void {
  for (const key of keys) {
    required(source, key);
  }
}


function decodeCapabilities(source: Record<string, unknown>): ChargerCapabilities {
  return {
    auto_price: booleanValue(source, "auto_price"),
    current_limit: booleanValue(source, "current_limit"),
    set_current: booleanValue(source, "set_current"),
    regulated_current: booleanValue(source, "regulated_current"),
    target_soc: booleanValue(source, "target_soc"),
    load_balancing: booleanValue(source, "load_balancing"),
    refresh_vehicle: booleanValue(source, "refresh_vehicle"),
    set_charge_limit: booleanValue(source, "set_charge_limit"),
    target_stop: booleanValue(source, "target_stop"),
  };
}

function decodeCharger(source: Record<string, unknown>): DashboardCharger {
  return {
    charger_id: text(source, "charger_id"),
    charger_name: textOrNull(source, "charger_name"),
    available: booleanValue(source, "available"),
    capabilities: decodeCapabilities(record(source.capabilities)),
  };
}

/**
 * Display-only here (the editor reads the canonical record), so an unreadable fiscal section
 * degrades to "no figures" instead of refusing the dashboard.
 */
function decodeFiscal(raw: unknown): Fiscal | null {
  if (!isRecord(raw)) {
    return null;
  }
  const component = (value: unknown): FiscalComponent | null => {
    if (!isRecord(value) || typeof value["policy"] !== "string") {
      return null;
    }
    const effective = value["effective_value"];
    const unit = value["unit"];
    return {
      policy: value["policy"],
      effective: typeof effective === "number" && Number.isFinite(effective) ? effective : null,
      unit: typeof unit === "string" ? unit : null,
    };
  };
  const vat = component(raw["vat"]);
  const tax = component(raw["tax"]);
  const transfer = component(raw["transfer"]);
  return vat === null || tax === null || transfer === null ? null : { vat, tax, transfer };
}

const PLANNING_KEYS = [
  "state",
  "reason",
  "settings_revision",
  "generation",
  "calculated_at",
  "historical",
  "missing",
  "price_state",
  "price_identity",
  "today",
  "tomorrow",
  "execution_state",
  "execution_reason",
  "applied",
  "applied_identity",
  "pending_identity",
  "pending_attempt",
  "execution_paused",
  "price_wait",
  "publication_at",
  "must_buy_now_kwh",
] as const;

function decodePlanning(source: Record<string, unknown>): Planning {
  presentKeys(source, PLANNING_KEYS);
  return {
    state: textOrNull(source, "state"),
    reason: textOrNull(source, "reason"),
    settings_revision: numberOrNull(source, "settings_revision"),
    price_state: textOrNull(source, "price_state"),
    execution_state: textOrNull(source, "execution_state"),
    execution_reason: textOrNull(source, "execution_reason"),
    execution_paused: booleanOrNull(source, "execution_paused"),
    applied: booleanOrNull(source, "applied"),
    missing: stringListOrNull(source, "missing"),
    today: textOrNull(source, "today"),
    tomorrow: textOrNull(source, "tomorrow"),
    price_wait: textOrNull(source, "price_wait"),
    publication_at: textOrNull(source, "publication_at"),
    must_buy_now_kwh: numberOrNull(source, "must_buy_now_kwh"),
  };
}

const MARKET_KEYS = [
  "catalogue_state",
  "catalogue_fetched_at",
  "catalogue_attempt_error",
  "area_id",
  "area_name",
  "countries",
  "timezone",
  "currency",
  "major_unit",
  "minor_unit",
  "suggested_vat_percent",
  "suggested_tax",
  "suggested_grid_fee",
] as const;

function decodeMarket(source: Record<string, unknown>): DashboardMarket {
  presentKeys(source, MARKET_KEYS);
  const timezone = textOrNull(source, "timezone");
  if (timezone !== null && !isValidTimeZone(timezone)) {
    bad();
  }
  return {
    catalogue_state: textOrNull(source, "catalogue_state"),
    area_id: textOrNull(source, "area_id"),
    area_name: textOrNull(source, "area_name"),
    countries: stringListOrNull(source, "countries"),
    timezone,
    currency: textOrNull(source, "currency"),
    major_unit: textOrNull(source, "major_unit"),
    minor_unit: textOrNull(source, "minor_unit"),
    suggested_vat_percent: numberOrNull(source, "suggested_vat_percent"),
  };
}

function decodeInterval(
  raw: unknown,
  timeZone: string | null,
  previous: PriceInterval | null,
): PriceInterval {
  const source = record(raw);
  const start = instant(source, "start");
  const end = instant(source, "end");
  const day = text(source, "day");
  const duration = numberValue(source, "duration_minutes");
  if (end.ms <= start.ms) {
    bad(); // an interval that does not advance is not an interval
  }
  if (!(duration > 0)) {
    bad();
  }
  if (Math.abs(end.ms - start.ms - duration * 60_000) > DURATION_TOLERANCE_MS) {
    bad(); // the stated resolution must describe the stated span
  }
  if (timeZone !== null && localDayKey(start.ms, timeZone) !== day) {
    bad(); // a row that claims another local day than its own start
  }
  if (previous !== null && start.ms <= previous.startMs) {
    bad(); // unique, chronological starts
  }
  if (previous !== null && start.ms < previous.endMs) {
    bad(); // and no overlap
  }
  return {
    start: start.text,
    startMs: start.ms,
    end: end.text,
    endMs: end.ms,
    day,
    duration_minutes: duration,
    raw_price: numberOrNull(source, "raw_price"),
    effective_price: numberOrNull(source, "effective_price"),
    proposal_planned: booleanValue(source, "proposal_planned"),
    installed_planned: booleanValue(source, "installed_planned"),
  };
}

const PRICES_KEYS = [
  "area_id",
  "state",
  "reason",
  "today",
  "tomorrow",
  "today_state",
  "tomorrow_state",
  "today_source",
  "tomorrow_source",
  "today_fetched_at",
  "tomorrow_fetched_at",
  "today_attempt_at",
  "tomorrow_attempt_at",
  "today_attempt_error",
  "tomorrow_attempt_error",
  "index_state",
  "index_revision",
  "waiting_for_tomorrow",
  "resolution_minutes",
  "resolutions_minutes",
  "priced_slots",
  "unpriced_slots",
  "unpriced",
  "interval_count",
  "intervals",
] as const;

function decodePrices(source: Record<string, unknown>, timeZone: string | null): Prices {
  presentKeys(source, PRICES_KEYS);
  const intervals: PriceInterval[] = [];
  let previous: PriceInterval | null = null;
  for (const raw of arrayValue(source, "intervals")) {
    const next = decodeInterval(raw, timeZone, previous);
    intervals.push(next);
    previous = next;
  }
  return {
    state: textOrNull(source, "state"),
    reason: textOrNull(source, "reason"),
    waiting_for_tomorrow: booleanOrNull(source, "waiting_for_tomorrow"),
    unpriced: booleanOrNull(source, "unpriced"),
    interval_count: numberValue(source, "interval_count"),
    priced_slots: numberOrNull(source, "priced_slots"),
    unpriced_slots: numberOrNull(source, "unpriced_slots"),
    resolution_minutes: numberOrNull(source, "resolution_minutes"),
    resolutions_minutes: numberList(source, "resolutions_minutes"),
    intervals,
  };
}

function decodePeriods(source: Record<string, unknown>): Period[] {
  const periods: Period[] = [];
  for (const raw of arrayValue(source, "periods")) {
    const entry = record(raw);
    const start = instant(entry, "start");
    const end = instant(entry, "end");
    if (end.ms <= start.ms) {
      bad();
    }
    periods.push({ start: start.text, startMs: start.ms, end: end.text, endMs: end.ms });
  }
  return periods.sort((left, right) => left.startMs - right.startMs);
}

function decodeProposal(source: Record<string, unknown>): Proposal {
  presentKeys(source, [
    "identity",
    "settings_revision",
    "periods",
    "amps",
    "phases",
    "power_kw",
    "requested_kwh",
    "planned_kwh",
    "cost",
    "distance_mil",
    "unpriced",
    "unpriced_slots",
    "priced_slots",
  ]);
  const cost = required(source, "cost");
  return {
    identity: textOrNull(source, "identity"),
    settings_revision: numberOrNull(source, "settings_revision"),
    periods: decodePeriods(source),
    amps: numberOrNull(source, "amps"),
    phases: numberOrNull(source, "phases"),
    unpriced: booleanValue(source, "unpriced"),
    unpriced_slots: numberOrNull(source, "unpriced_slots"),
    priced_slots: numberOrNull(source, "priced_slots"),
    planned_kwh: numberOrNull(source, "planned_kwh"),
    requested_kwh: numberOrNull(source, "requested_kwh"),
    cost:
      cost === null
        ? null
        : {
            value: numberValue(record(cost), "value"),
            currency: textOrNull(record(cost), "currency"),
          },
    distance_mil: numberOrNull(source, "distance_mil"),
    power_kw: numberOrNull(source, "power_kw"),
  };
}

function decodeInstalled(source: Record<string, unknown>): Installed {
  return {
    identity: textOrNull(source, "identity"),
    periods: decodePeriods(source),
    amps: numberOrNull(source, "amps"),
    phases: numberOrNull(source, "phases"),
    power_kw: numberOrNull(source, "power_kw"),
    active_period_index: numberOrNull(source, "active_period_index"),
  };
}

function decodeRelation(source: Record<string, unknown>): Relation {
  presentKeys(source, ["applied", "applied_identity", "pending_identity"]);
  return {
    applied: booleanOrNull(source, "applied"),
    applied_identity: textOrNull(source, "applied_identity"),
    pending_identity: textOrNull(source, "pending_identity"),
  };
}

/**
 * `serialize_plan()` always emits these keys. `proposal` and `installed` are nullable but required:
 * read through `required()` so a missing key is not mistaken for a real `null`.
 */
const PLAN_KEYS = [
  "proposal",
  "installed",
  "relation",
  "delivered_kwh",
  "remaining_kwh",
] as const;

function decodePlan(source: Record<string, unknown>): Plan {
  presentKeys(source, PLAN_KEYS);
  const proposal = required(source, "proposal");
  const installed = required(source, "installed");
  return {
    proposal: proposal === null ? null : decodeProposal(record(proposal)),
    installed: installed === null ? null : decodeInstalled(record(installed)),
    relation: decodeRelation(record(required(source, "relation"))),
    delivered_kwh: numberOrNull(source, "delivered_kwh"),
    remaining_kwh: numberOrNull(source, "remaining_kwh"),
  };
}

function decodeLive(source: Record<string, unknown>): Live {
  return {
    charging: booleanOrNull(source, "charging"),
    schedule_active: booleanOrNull(source, "schedule_active"),
    requested_current_a: numberOrNull(source, "requested_current_a"),
    setpoint_current_a: numberOrNull(source, "setpoint_current_a"),
    measured_current_a: numberOrNull(source, "measured_current_a"),
  };
}

const PAUSE_CHOICES = ["next_period", "until_tomorrow", "until_resumed"] as const;

const STRATEGIES = ["cheapest", "solar", "hybrid"] as const;
const UNAVAILABLE_STRATEGIES = ["solar", "hybrid"] as const;

/**
 * Closed set of reasons the two capability placeholders may carry; an unknown one is malformed
 * because the card could not translate it.
 */
const STRATEGY_REASONS = [
  "needs_solar_surplus_measurement",
  "needs_solar_and_price_control",
  "needs_total_grid_power",
] as const;

/**
 * Closed vocabularies of the two axes. The immediate axis knows no pause; the automatic axis never
 * names start/stop. Separate sets so one axis's word on the other is malformed rather than a button
 * the boundary would refuse.
 */
const IMMEDIATE_ACTIONS = [ACTION_START, ACTION_STOP, "none"] as const;
const AUTOMATIC_ACTIONS = [ACTION_PAUSE, ACTION_RESUME, "none"] as const;

/**
 * Reasons each axis may carry when it has no action. The immediate axis is blind to planning; the
 * automatic axis speaks the planning record's whole vocabulary. A reason from the wrong axis is
 * malformed.
 */
const IMMEDIATE_REASONS = ["no_settings", "action_pending"] as const;
const AUTOMATIC_REASONS = [
  "no_settings",
  "pause_unsettled",
  "pause_clear_failed",
  "action_pending",
] as const;

function instantOrNull(source: Record<string, unknown>, key: string): [string | null, number | null] {
  const value = required(source, key);
  if (value === null) {
    return [null, null];
  }
  if (typeof value !== "string" || !OFFSET_SUFFIX.test(value)) {
    return bad();
  }
  const milliseconds = Date.parse(value);
  if (Number.isNaN(milliseconds)) {
    return bad();
  }
  return [value, milliseconds];
}

function enumOrNull<T extends string>(
  source: Record<string, unknown>,
  key: string,
  allowed: readonly T[],
): T | null {
  const value = required(source, key);
  if (value === null) {
    return null;
  }
  if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) {
    return bad();
  }
  return value as T;
}

/**
 * One pause observation: exactly three keys, in a combination that could describe a real intent.
 * Mirrors the store's `PauseIntent` validation: no pause is three nulls, `until_resumed` has no
 * expiry, a timed choice carries one. Whether it is paused is the control block's answer, not
 * `expires_at`.
 */
function decodePause(source: Record<string, unknown>): Pause {
  exactKeys(source, ["choice", "admitted_at", "expires_at"]);
  const choice = enumOrNull(source, "choice", PAUSE_CHOICES);
  const [admittedAt, admittedAtMs] = instantOrNull(source, "admitted_at");
  const [expiresAt, expiresAtMs] = instantOrNull(source, "expires_at");
  if (choice === null) {
    if (admittedAt !== null || expiresAt !== null) {
      return bad();
    }
    return {
      choice,
      admitted_at: null,
      admitted_at_ms: null,
      expires_at: null,
      expires_at_ms: null,
    };
  }
  // The admission instant is optional: a pause may exist without a known start, and the browser must
  // not invent one. A timed pause needs an end instant, and cannot end before it was admitted.
  if (choice === "until_resumed" ? expiresAt !== null : expiresAt === null) {
    return bad();
  }
  if (admittedAtMs !== null && expiresAtMs !== null && expiresAtMs <= admittedAtMs) {
    return bad();
  }
  return {
    choice,
    admitted_at: admittedAt,
    admitted_at_ms: admittedAtMs,
    expires_at: expiresAt,
    expires_at_ms: expiresAtMs,
  };
}

function decodeStrategyRow(source: Record<string, unknown>): StrategyRow {
  exactKeys(source, ["strategy", "available", "reason"]);
  const strategy = enumOrNull(source, "strategy", STRATEGIES);
  const available = booleanValue(source, "available");
  const reason = textOrNull(source, "reason");
  if (strategy === null) {
    return bad();
  }
  // A row is either on with nothing to explain, or a placeholder that is off and says why.
  if (available && reason !== null) {
    return bad();
  }
  if (
    !available &&
    (reason === null ||
      !(STRATEGY_REASONS as readonly string[]).includes(reason) ||
      !(UNAVAILABLE_STRATEGIES as readonly string[]).includes(strategy))
  ) {
    return bad();
  }
  return { strategy, available, reason };
}

function decodeStrategy(source: Record<string, unknown>): Strategy {
  exactKeys(source, ["selected", "available"]);
  const selected = enumOrNull(source, "selected", STRATEGIES);
  const rows = arrayValue(source, "available").map((row) => decodeStrategyRow(record(row)));
  const seen = new Set<string>();
  for (const row of rows) {
    if (seen.has(row.strategy)) {
      return bad();
    }
    seen.add(row.strategy);
  }
  // The selected strategy's row must say it is on. Several rows may be available at once, so only
  // that `selected` is one of them is checked, and nothing may claim availability when nothing is
  // selected.
  const enabled = rows.filter((row) => row.available).map((row) => row.strategy);
  if (selected === null ? enabled.length !== 0 : !enabled.includes(selected)) {
    return bad();
  }
  return { selected, rows };
}

function decodeControl(source: Record<string, unknown>): Control {
  exactKeys(source, [
    "immediate_action",
    "immediate_action_reason",
    "automatic_action",
    "automatic_action_reason",
    "pause_choices",
    "pause",
    "pause_blocks_execution",
    "execution_error",
    "can_act",
  ]);
  const immediate = enumOrNull(source, "immediate_action", IMMEDIATE_ACTIONS);
  const automatic = enumOrNull(source, "automatic_action", AUTOMATIC_ACTIONS);
  if (immediate === null || automatic === null) {
    return bad();
  }
  const immediateReason = enumOrNull(source, "immediate_action_reason", IMMEDIATE_REASONS);
  const automaticReason = enumOrNull(source, "automatic_action_reason", AUTOMATIC_REASONS);
  // An actionable axis has no reason, and `none` always has one.
  if (immediate === "none" ? immediateReason === null : immediateReason !== null) {
    return bad();
  }
  if (automatic === "none" ? automaticReason === null : automaticReason !== null) {
    return bad();
  }
  const choices: string[] = arrayValue(source, "pause_choices").map((value) => {
    if (typeof value !== "string" || !(PAUSE_CHOICES as readonly string[]).includes(value)) {
      return bad();
    }
    return value;
  });
  if (new Set(choices).size !== choices.length) {
    return bad();
  }
  // Only the automatic pause offers choices; an immediate `stop` carries none.
  if (automatic !== ACTION_PAUSE && choices.length > 0) {
    return bad();
  }
  const blocks = booleanValue(source, "pause_blocks_execution");
  const rawPause = required(source, "pause");
  const pause = rawPause === null ? null : decodePause(record(rawPause));
  if (pause === null) {
    // Missing observation is itself the fact only in the no-settings state: neither axis names an
    // action, nothing is blocked or to choose, and there is no execution failure.
    if (
      immediate !== "none" ||
      immediateReason !== "no_settings" ||
      automatic !== "none" ||
      automaticReason !== "no_settings" ||
      blocks ||
      choices.length > 0
    ) {
      return bad();
    }
  } else if (blocks !== (pause.choice !== null)) {
    // The gate and the record are two views of `pause.admitted`: they must agree.
    return bad();
  }
  if (automaticReason === "pause_clear_failed" && (pause === null || pause.choice === null)) {
    // An elapsed-but-uncleared pause is a persisted pause whose clearing write failed; without one
    // there is nothing that failed.
    return bad();
  }
  return {
    immediate_action: immediate,
    immediate_action_reason: immediateReason,
    automatic_action: automatic,
    automatic_action_reason: automaticReason,
    pause_choices: choices,
    pause,
    pause_blocks_execution: blocks,
    execution_error: textOrNull(source, "execution_error"),
    can_act: booleanValue(source, "can_act"),
  };
}

const SOLAR_STRATEGY_STATE_KEYS = [
  "state",
  "reason",
  "available_w",
  "requested_a",
  "priority_effective",
] as const;

const HYBRID_STRATEGY_STATE_KEYS = [
  "state",
  "reason",
  "grid_kwh",
  "credit_kwh",
  "slack_kwh",
  "plan_window_active",
  "forecast_configured",
] as const;

/**
 * The `strategy_state`, checked against the sibling `selected` strategy.
 *
 * The wire has no discriminant: the value's shape must agree with `selected` (`null` for `cheapest`
 * and no settings record, solar shape for `solar`, hybrid shape for `hybrid`). Any disagreement is
 * refused like every other cross-field contradiction.
 */
function decodeStrategyState(
  root: Record<string, unknown>,
  selected: string | null,
): StrategyState | null {
  const raw = required(root, "strategy_state");
  if (selected !== "solar" && selected !== "hybrid") {
    return raw === null ? null : bad();
  }
  if (raw === null) {
    return bad();
  }
  const value = record(raw);
  if (selected === "solar") {
    exactKeys(value, SOLAR_STRATEGY_STATE_KEYS);
    return {
      kind: "solar",
      state: text(value, "state"),
      reason: textOrNull(value, "reason"),
      available_w: numberOrNull(value, "available_w"),
      requested_a: numberOrNull(value, "requested_a"),
      priority_effective: textOrNull(value, "priority_effective"),
    };
  }
  exactKeys(value, HYBRID_STRATEGY_STATE_KEYS);
  return {
    kind: "hybrid",
    state: text(value, "state"),
    reason: textOrNull(value, "reason"),
    grid_kwh: numberOrNull(value, "grid_kwh"),
    credit_kwh: numberOrNull(value, "credit_kwh"),
    slack_kwh: numberOrNull(value, "slack_kwh"),
    plan_window_active: booleanValue(value, "plan_window_active"),
    forecast_configured: booleanValue(value, "forecast_configured"),
  };
}

const SOLAR_PRIORITIES = ["car_first", "battery_first"] as const;

function decodeSolarForecastChoice(source: Record<string, unknown>): SolarForecastChoice {
  exactKeys(source, ["id", "title"]);
  return { id: text(source, "id"), title: text(source, "title") };
}

/**
 * The `site` section beside `load_balancing`. Exact keys throughout; ids and titles are opaque wire
 * text the card never interprets.
 */
/**
 * Exported for `site-settings.ts`: `spotnav/update_site_settings` answers with the same `site` shape,
 * so one decoder serves both.
 */
export function decodeSite(source: Record<string, unknown>): Site {
  exactKeys(source, [
    "name",
    "charger_count",
    "solar_priority",
    "solar_forecast",
    "active_control",
    "writable",
  ]);
  const priority = enumOrNull(source, "solar_priority", SOLAR_PRIORITIES);
  if (priority === null) {
    return bad();
  }
  const forecast = record(required(source, "solar_forecast"));
  exactKeys(forecast, ["selected", "choices"]);
  const selectedIds = arrayValue(forecast, "selected").map((entry) =>
    typeof entry === "string" ? entry : bad(),
  );
  const choices = arrayValue(forecast, "choices").map((entry) => decodeSolarForecastChoice(record(entry)));
  const choiceIds = new Set(choices.map((choice) => choice.id));
  if (new Set(choiceIds).size !== choices.length || selectedIds.some((id) => !choiceIds.has(id))) {
    return bad();
  }
  const activeControl = record(required(source, "active_control"));
  exactKeys(activeControl, ["available", "enabled", "reason", "writable"]);
  const available = booleanValue(activeControl, "available");
  const reason = textOrNull(activeControl, "reason");
  if (available && reason !== null) {
    return bad();
  }
  return {
    name: text(source, "name"),
    charger_count: numberValue(source, "charger_count"),
    solar_priority: priority,
    solar_forecast: { selected: selectedIds, choices },
    active_control: {
      available,
      enabled: booleanValue(activeControl, "enabled"),
      reason,
      writable: booleanValue(activeControl, "writable"),
    },
    writable: booleanValue(source, "writable"),
  };
}


export type StatusTone = "normal" | "notice" | "blocking";
export const STATUS_TONES: readonly StatusTone[] = ["normal", "notice", "blocking"];

export type StatusParam = string | number | string[] | null;

type StatusParamKind = "text" | "textOrNull" | "instant" | "instantOrNull" | "number" | "numberOrNull" | "int" | "codes" | "choiceOrNull";

/**
 * Every status code Home Assistant composes (`status_compose.STATUS_CODES`), with its tone and typed
 * params. `test/issue-codes.test.ts` holds this table to the backend's emitted-codes fixture; a code
 * outside it is refused by the decoder.
 */
export const STATUS_CODE_TABLE = {
  charger_unavailable: ["blocking", {}],
  price_data_invalid: ["blocking", { reason: "textOrNull" }],
  price_data_unavailable: ["blocking", { reason: "textOrNull" }],
  settings_incomplete: ["notice", { reason: "textOrNull", missing: "codes" }],
  solar_unavailable: ["blocking", {}],
  target_soc_unknown: ["blocking", { missing: "codes" }],
  price_horizon_missing: ["blocking", {}],
  planning_unavailable: ["blocking", { reason: "textOrNull" }],
  planning_error: ["blocking", { reason: "textOrNull" }],
  paused: ["normal", { until: "instantOrNull", choice: "choiceOrNull" }],
  charging_now: ["normal", { until: "instantOrNull" }],
  charging_without_prices: ["notice", {}],
  waiting_for_publication: ["normal", { publication_at: "instantOrNull" }],
  waiting_for_history: ["normal", { weekday: "int", percent: "int", weeks: "int" }],
  buying_before_publication: ["normal", { kwh: "number" }],
  auto_planned: ["normal", { start: "instant" }],
  auto_installed: ["normal", { start: "instant" }],
  proposal_pending: ["normal", {}],
  waiting_for_tomorrow: ["normal", {}],
  no_plan: ["normal", {}],
  nothing_to_charge: ["normal", {}],
  plan_energy: ["normal", { kwh: "number" }],
  plan_cost: ["normal", { amount_minor: "int", currency: "text" }],
  plan_distance: ["normal", { mil: "number" }],
  solar_charging: ["normal", { requested_a: "numberOrNull" }],
  solar_arming: ["normal", {}],
  solar_disarming: ["normal", {}],
  solar_no_reading_stopped: ["normal", {}],
  solar_no_reading_waiting: ["normal", {}],
  solar_waiting_for_sun: ["normal", {}],
  solar_unknown: ["normal", {}],
  hybrid_grid: [
    "normal",
    { grid_kwh: "number", credit_kwh: "numberOrNull", window_start: "instantOrNull", window_end: "instantOrNull" },
  ],
  hybrid_no_forecast: ["normal", {}],
  hybrid_no_price_data: ["normal", {}],
  hybrid_satisfied: ["normal", {}],
  hybrid_unknown: ["normal", {}],
  settings_suggested: ["normal", { fields: "codes" }],
  target_reached: ["normal", { soc_percent: "number", basis: "text", reading_age_s: "numberOrNull" }],
  target_unverifiable: ["notice", { reason: "text" }],
  price_data_stale: ["notice", { reason: "textOrNull" }],
  price_data_degraded: ["notice", { reason: "textOrNull" }],
  unpriced: ["notice", {}],
  load_balancing_limited: ["notice", { limit_a: "numberOrNull", phase: "textOrNull" }],
  load_balancing_unavailable: ["notice", {}],
  held_by_charger: ["notice", {}],
  charger_disabled: ["notice", {}],
  held_until_window: ["normal", { time: "instant" }],
  hold_overridden: ["notice", {}],
} as const satisfies Record<string, readonly [StatusTone, Record<string, StatusParamKind>]>;

export type StatusCode = keyof typeof STATUS_CODE_TABLE;

export interface StatusLine {
  code: StatusCode;
  params: Readonly<Record<string, StatusParam>>;
}

export interface Status {
  tone: StatusTone;
  lines: StatusLine[];
}

function decodeStatusParam(source: Record<string, unknown>, key: string, kind: StatusParamKind): StatusParam {
  switch (kind) {
    case "text":
      return text(source, key);
    case "textOrNull":
      return textOrNull(source, key);
    case "instant": {
      const [iso] = instantOrNull(source, key);
      return iso ?? bad();
    }
    case "instantOrNull":
      return instantOrNull(source, key)[0];
    case "number":
      return numberValue(source, key);
    case "numberOrNull":
      return numberOrNull(source, key);
    case "int": {
      const value = numberValue(source, key);
      return Number.isInteger(value) ? value : bad();
    }
    case "choiceOrNull":
      return enumOrNull(source, key, PAUSE_CHOICES);
    case "codes":
      return arrayValue(source, key).map((entry) => (typeof entry === "string" ? entry : bad()));
  }
}

function decodeStatus(source: Record<string, unknown>): Status {
  exactKeys(source, ["tone", "lines"]);
  const tone = text(source, "tone");
  if (!(STATUS_TONES as readonly string[]).includes(tone)) {
    return bad();
  }
  let blocking = false;
  const lines = arrayValue(source, "lines").map((raw): StatusLine => {
    const line = record(raw);
    exactKeys(line, ["code", "params"]);
    const code = text(line, "code");
    if (!Object.prototype.hasOwnProperty.call(STATUS_CODE_TABLE, code)) {
      return bad();
    }
    const [lineTone, kinds] = STATUS_CODE_TABLE[code as StatusCode] as readonly [StatusTone, Record<string, StatusParamKind>];
    const params = record(line.params);
    exactKeys(params, Object.keys(kinds));
    blocking ||= lineTone === "blocking";
    return {
      code: code as StatusCode,
      params: Object.fromEntries(
        Object.entries(kinds).map(([key, kind]) => [key, decodeStatusParam(params, key, kind)]),
      ),
    };
  });
  // A blocking line under any other tone contradicts itself: refused, not reconciled.
  if (blocking !== (tone === "blocking")) {
    return bad();
  }
  return { tone: tone as StatusTone, lines };
}

/**
 * Decode one dashboard answer. A wrong `api_version` is `unsupported`; anything else that does not
 * fit is `malformed`. Neither carries payload text or an exception message.
 */
export function decodeDashboard(raw: unknown): DecodeResult {
  try {
    const root = isRecord(raw) ? raw : bad();
    const version = root.api_version;
    if (typeof version !== "number" || !Number.isFinite(version)) {
      return { ok: false, failure: "malformed" };
    }
    if (version !== API_VERSION) {
      return { ok: false, failure: "unsupported" };
    }
    exactKeys(root, DASHBOARD_KEYS);
    const market = sectionOrNull(root, "market", decodeMarket);
    const timeZone = market?.timezone ?? null;
    const strategy = decodeStrategy(record(required(root, "strategy")));
    const settings = required(root, "settings");
    return {
      ok: true,
      value: {
        api_version: 1,
        generated_at: textOrNull(root, "generated_at"),
        charger: decodeCharger(record(root.charger)),
        settings: settings === null ? null : decodeSettingsRecord(settings),
        fiscal: decodeFiscal(root["fiscal"]),
        planning: sectionOrNull(root, "planning", decodePlanning),
        market,
        prices: decodePrices(record(required(root, "prices")), timeZone),
        plan: decodePlan(record(required(root, "plan"))),
        live: decodeLive(record(required(root, "live"))),
        strategy,
        strategy_options: strategyOptions(root),
        strategy_state: decodeStrategyState(root, strategy.selected),
        control: decodeControl(record(required(root, "control"))),
        charge_progress: progressOrNull(root),
        site: sectionOrNull(root, "site", decodeSite),
        current_range: currentRangeOrNull(root),
        soc: socOrNull(root),
        vehicles: arrayValue(root, "vehicles").map(decodeVehicle),
        target_vehicle_id: textOrNull(root, "target_vehicle_id"),
        detected_phases: detectedPhases(root),
        phase_detection: decodePhaseDetection(record(required(root, "phase_detection"))),
        chargers: arrayValue(root, "chargers").map((entry) => {
          const item = record(entry);
          exactKeys(item, ["id", "name"]);
          return { id: text(item, "id"), name: text(item, "name") };
        }),
        status: decodeStatus(record(required(root, "status"))),
      },
    };
  } catch {
    return { ok: false, failure: "malformed" };
  }
}

const DASHBOARD_KEYS = [
  "api_version",
  "generated_at",
  "charger",
  "settings",
  "fiscal",
  "planning",
  "market",
  "prices",
  "plan",
  "live",
  "strategy",
  "strategy_options",
  "strategy_state",
  "control",
  "charge_progress",
  "site",
  "current_range",
  "soc",
  "vehicles",
  "target_vehicle_id",
  "detected_phases",
  "phase_detection",
  "chargers",
  "status",
  // The setup in words, for a client other than this card (which builds the same lines from the
  // entity configuration); accepted and not read.
  "summary",
] as const;

function strategyOptions(root: Record<string, unknown>): string[] {
  const options = arrayValue(root, "strategy_options").map((entry) =>
    typeof entry === "string" && (STRATEGIES as readonly string[]).includes(entry) ? entry : bad(),
  );
  return new Set(options).size === options.length ? options : bad();
}

function detectedPhases(root: Record<string, unknown>): number | null {
  const value = numberOrNull(root, "detected_phases");
  return value === null || value === 1 || value === 3 ? value : bad();
}

function decodePhaseDetection(source: Record<string, unknown>): PhaseDetection {
  exactKeys(source, ["source", "confidence"]);
  return { source: text(source, "source"), confidence: text(source, "confidence") };
}

/**
 * The `current_range`, or `null` when the block is unreadable. Independent like `charge_progress`:
 * exact keys, whole amps from 1 to 80 with the top not below the bottom.
 */
function currentRangeOrNull(root: Record<string, unknown>): ChargerCurrentRange | null {
  const value = root.current_range;
  if (!isRecord(value)) {
    return null;
  }
  try {
    exactKeys(value, ["min_a", "max_a", "source"]);
    const min = value.min_a;
    const max = value.max_a;
    if (
      typeof min !== "number" ||
      typeof max !== "number" ||
      !Number.isInteger(min) ||
      !Number.isInteger(max) ||
      min < 1 ||
      max > 80 ||
      max < min
    ) {
      return null;
    }
    return { min_a: min, max_a: max, source: text(value, "source") };
  } catch {
    return null;
  }
}

const SOC_MISSING = ["soc", "capacity", "vehicle"] as const;
const SOC_SOURCES = ["charger", "vehicle"] as const;

function boundedOrNull(
  source: Record<string, unknown>,
  key: string,
  minimum: number,
  maximum: number,
  exclusiveMinimum = false,
): number | null {
  const value = numberOrNull(source, key);
  if (value === null) {
    return null;
  }
  return value > maximum || value < minimum || (exclusiveMinimum && value === minimum) ? bad() : value;
}

/**
 * The `soc` block. `null` means no source resolves; a present block is exact-keyed and range-judged,
 * and anything else throws, refusing the whole document.
 */
function socOrNull(root: Record<string, unknown>): Soc | null {
  if (root.soc === null) {
    return null;
  }
  const source = record(root.soc);
  exactKeys(source, [
    "value",
    "age_s",
    "source",
    "estimated",
    "target_percent",
    "need_kwh",
    "capacity_kwh",
    "vehicle_name",
    "vehicle_id",
    "vehicles",
    "vehicle_max_percent",
    "efficiency",
    "missing",
  ]);
  const missing = arrayValue(source, "missing").map((entry) =>
    (SOC_MISSING as readonly unknown[]).includes(entry) ? (entry as "soc" | "capacity" | "vehicle") : bad(),
  );
  if (new Set(missing).size !== missing.length) {
    return bad();
  }
  return {
    value: boundedOrNull(source, "value", 0, 100),
    age_s: boundedOrNull(source, "age_s", 0, Number.POSITIVE_INFINITY),
    source: enumOrNull(source, "source", SOC_SOURCES),
    estimated: booleanValue(source, "estimated"),
    target_percent: boundedOrNull(source, "target_percent", 0, 100),
    need_kwh: boundedOrNull(source, "need_kwh", 0, Number.POSITIVE_INFINITY),
    capacity_kwh: boundedOrNull(source, "capacity_kwh", 0, Number.POSITIVE_INFINITY, true),
    vehicle_name: textOrNull(source, "vehicle_name"),
    vehicle_id: textOrNull(source, "vehicle_id"),
    vehicles: arrayValue(source, "vehicles").map((entry) => {
      const item = record(entry);
      exactKeys(item, ["id", "name"]);
      return { id: text(item, "id"), name: text(item, "name") };
    }),
    vehicle_max_percent: boundedOrNull(source, "vehicle_max_percent", 0, 100),
    efficiency: boundedOrNull(source, "efficiency", 0, 1, true) ?? bad(),
    missing,
  };
}

const CAPACITY_SOURCES = ["reported", "stored"] as const;

export function decodeVehicle(raw: unknown): Vehicle {
  const source = record(raw);
  exactKeys(source, [
    "id",
    "name",
    "soc_entity_id",
    "capacity_kwh",
    "capacity_source",
    "consumption_kwh_per_10km",
    "max_percent",
    "soc_percent",
  ]);
  const capacity = boundedOrNull(source, "capacity_kwh", 0, Number.POSITIVE_INFINITY, true);
  const origin = enumOrNull(source, "capacity_source", CAPACITY_SOURCES);
  if ((capacity === null) !== (origin === null)) {
    return bad();
  }
  return {
    id: text(source, "id"),
    name: textOrNull(source, "name"),
    soc_entity_id: textOrNull(source, "soc_entity_id"),
    capacity_kwh: capacity,
    capacity_source: origin,
    consumption_kwh_per_10km: boundedOrNull(source, "consumption_kwh_per_10km", 0, Number.POSITIVE_INFINITY, true),
    max_percent: boundedOrNull(source, "max_percent", 0, 100),
    soc_percent: boundedOrNull(source, "soc_percent", 0, 100),
  };
}

/**
 * The `charge_progress`, or `null` when the block is unreadable. Refused independently: a missing,
 * mis-shaped, extra-keyed or unknown-state value hides the advisory and leaves the rest of the
 * document as decoded.
 */
function progressOrNull(root: Record<string, unknown>): ChargeProgress | null {
  const value = root.charge_progress;
  if (!isRecord(value)) {
    return null;
  }
  try {
    exactKeys(value, ["state", "reason", "since"]);
    const state = text(value, "state");
    if (!CHARGE_PROGRESS_STATES.includes(state as ChargeProgressState)) {
      return null;
    }
    return {
      state: state as ChargeProgressState,
      reason: text(value, "reason"),
      since: textOrNull(value, "since"),
    };
  } catch {
    return null;
  }
}
