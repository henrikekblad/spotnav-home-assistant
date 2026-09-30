// The settings contract on the client: wire shapes, one strict decoder, and the pure edits the
// compact dialogs perform.
//
// Three API versions live in this card: the dashboard's `API_VERSION` (graph and status), the action
// contract's `ACTION_API_VERSION` (Start/Stop/Resume) and this file's `SETTINGS_API_VERSION` (edits).
//
// Nothing is edited from the dashboard's reduced `settings` section. Every dialog reads the canonical
// record over `spotnav/get_settings`, keeps it as an immutable base, and saves one full replacement
// built from it, so fields a focused edit does not own cannot be lost.

import { energyAmount, formatNumber, percentAmount } from "./format";
import { translate, type Language, type TranslationKey } from "./i18n";
import {
  SETTINGS_API_VERSION,
  SETTINGS_DRIVER_TARGET_SOC,
  SETTINGS_NOT_COMMITTED,
  SETTINGS_RECONCILE_FAILED,
  SETTINGS_STRATEGY_CHEAPEST,
  SETTINGS_STRATEGY_HYBRID,
  SETTINGS_STRATEGY_SOLAR,
  type AreaOverride,
  type PauseObservation,
  type SettingsAnswer,
  type SettingsBody,
  type SettingsRecord,
} from "./types";
import type { Fiscal } from "./validate";

class MalformedPayload extends Error {}

function bad(): never {
  throw new MalformedPayload("malformed");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : bad();
}

function exactKeys(source: Record<string, unknown>, keys: readonly string[]): void {
  if (Object.keys(source).length !== keys.length) {
    return bad();
  }
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) {
      return bad();
    }
  }
}

function text(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" ? value : bad();
}

function textOrNull(source: Record<string, unknown>, key: string): string | null {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "string" ? value : bad();
}

function booleanValue(source: Record<string, unknown>, key: string): boolean {
  const value = source[key];
  return typeof value === "boolean" ? value : bad();
}

function finite(source: Record<string, unknown>, key: string): number {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : bad();
}

function whole(source: Record<string, unknown>, key: string): number {
  const value = source[key];
  return typeof value === "number" && Number.isInteger(value) ? value : bad();
}

function wholeOrNull(source: Record<string, unknown>, key: string): number | null {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "number" && Number.isInteger(value) ? value : bad();
}

function oneOf<T extends string>(
  source: Record<string, unknown>,
  key: string,
  allowed: readonly T[],
): T {
  const value = source[key];
  if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) {
    return bad();
  }
  return value as T;
}

function list(source: Record<string, unknown>, key: string): unknown[] {
  const value = source[key];
  return Array.isArray(value) ? value : bad();
}

const WALL_TIME = /^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/;

function wallTime(source: Record<string, unknown>, key: string): string {
  const value = text(source, key);
  return WALL_TIME.test(value) ? value : bad();
}

function instantOrNull(source: Record<string, unknown>, key: string): string | null {
  const value = textOrNull(source, key);
  if (value === null) {
    return null;
  }
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/.test(value) || Number.isNaN(Date.parse(value))) {
    return bad();
  }
  return value;
}

const PAUSE_CHOICES = ["next_period", "until_tomorrow", "until_resumed"] as const;
const DRIVERS = ["manual_kwh", "target_soc"] as const;
/**
 * The writable strategy vocabulary (`STORED_STRATEGIES` in `auto_settings.py`). A backend that
 * discards the strategy echoes `cheapest`, which this list also covers.
 */
const STRATEGIES = [SETTINGS_STRATEGY_CHEAPEST, SETTINGS_STRATEGY_SOLAR, SETTINGS_STRATEGY_HYBRID] as const;
const FISCAL_KEYS = ["enabled", "value"] as const;
const OVERRIDE_KEYS = ["area_id", "vat", "tax", "transfer"] as const;
const TARGET_KEYS = ["vehicle_id", "target_percent"] as const;

const BODY_KEYS = [
  "area_id",
  "overrides",
  "phases",
  "amps",
  "requested_kwh",
  "max_periods",
  "departure_enabled",
  "departure_time",
  "strategy",
  "driver",
  "target",
] as const;

const RECORD_KEYS = [...BODY_KEYS, "revision"] as const;
const ENVELOPE_KEYS = ["api_version", "ok", "error", "settings", "pause"] as const;
const PAUSE_KEYS = ["choice", "admitted_at", "expires_at"] as const;

function decodeFiscal(source: Record<string, unknown>): { enabled: boolean; value: number | null } {
  exactKeys(source, FISCAL_KEYS);
  const enabled = booleanValue(source, "enabled");
  const value = source["value"];
  if (value !== null && !(typeof value === "number" && Number.isFinite(value))) {
    return bad();
  }
  return { enabled, value: value as number | null };
}

function decodeOverride(source: Record<string, unknown>): AreaOverride {
  exactKeys(source, OVERRIDE_KEYS);
  return {
    area_id: text(source, "area_id"),
    vat: decodeFiscal(record(source["vat"])),
    tax: decodeFiscal(record(source["tax"])),
    transfer: decodeFiscal(record(source["transfer"])),
  };
}

/**
 * The target record: the one place this decoder judges a range, and a decimal percentage.
 *
 * `target_percent` is a decimal (`float | None` in `0..100`, never rounded by the backend), so `80.5`
 * is valid and must stay copyable by other edits; reading it as a whole number would make the
 * editors unreachable. The range is judged because a percentage outside `0..100` must never be
 * copied into a replacement. Planning numbers keep their ranges on the server (see
 * `decodeSettingsRecord`), where the bounds are product policy.
 */
function decodeTarget(source: Record<string, unknown>): SettingsRecord["target"] {
  exactKeys(source, TARGET_KEYS);
  let targetPercent: number | null = null;
  if (source["target_percent"] !== null) {
    const percent = finite(source, "target_percent");
    if (percent < 0 || percent > 100) {
      return bad();
    }
    targetPercent = percent;
  }
  return {
    vehicle_id: textOrNull(source, "vehicle_id"),
    target_percent: targetPercent,
  };
}

/**
 * One canonical settings record: twelve keys, each with the type the contract promises.
 *
 * Ranges are deliberately not judged (the backend validates what it stores, and second-guessing a
 * planning bound would refuse valid payloads when policy moves); `decodeTarget` is the exception.
 * A non-conforming payload is refused whole, and unknown extra keys are refused too, because a
 * field the card cannot copy would be silently dropped on the next save.
 */
export function decodeSettingsRecord(raw: unknown): SettingsRecord {
  const source = record(raw);
  exactKeys(source, RECORD_KEYS);
  const revision = whole(source, "revision");
  if (revision < 0) {
    return bad();
  }
  return {
    revision,
    area_id: textOrNull(source, "area_id"),
    overrides: list(source, "overrides").map((item) => decodeOverride(record(item))),
    phases: wholeOrNull(source, "phases"),
    amps: wholeOrNull(source, "amps"),
    requested_kwh: finite(source, "requested_kwh"),
    max_periods: whole(source, "max_periods"),
    departure_enabled: booleanValue(source, "departure_enabled"),
    departure_time: wallTime(source, "departure_time"),
    strategy: oneOf(source, "strategy", STRATEGIES),
    driver: oneOf(source, "driver", DRIVERS),
    target: decodeTarget(record(source["target"])),
  };
}

/**
 * One pause observation: three keys, in a combination that could describe a real intent.
 *
 * Mirrors the store's validation: no pause is three nulls, `until_resumed` has no expiry, a timed
 * choice carries one, and a pause cannot end before it was admitted. An absent admission instant is
 * accepted.
 */
export function decodePauseObservation(raw: unknown): PauseObservation {
  const source = record(raw);
  exactKeys(source, PAUSE_KEYS);
  const choice = textOrNull(source, "choice");
  if (choice !== null && !(PAUSE_CHOICES as readonly string[]).includes(choice)) {
    return bad();
  }
  const admittedAt = instantOrNull(source, "admitted_at");
  const expiresAt = instantOrNull(source, "expires_at");
  if (choice === null) {
    return admittedAt !== null || expiresAt !== null
      ? bad()
      : { choice: null, admitted_at: null, expires_at: null };
  }
  if (choice === "until_resumed" ? expiresAt !== null : expiresAt === null) {
    return bad();
  }
  if (admittedAt !== null && expiresAt !== null && Date.parse(expiresAt) <= Date.parse(admittedAt)) {
    return bad();
  }
  return { choice, admitted_at: admittedAt, expires_at: expiresAt };
}

export type SettingsDecodeResult =
  | { ok: true; value: SettingsAnswer }
  | { ok: false; failure: "unsupported" | "malformed" };

function malformed(): SettingsDecodeResult {
  return { ok: false, failure: "malformed" };
}

/**
 * The one settings boundary: the answer is `unknown` until this accepts it.
 *
 * A different `api_version` is `unsupported`; every other disagreement is `malformed`; neither
 * carries payload text. A refusal answers a successful frame with `ok: false`, so a conflict or an
 * unreconciled write is a fact the card reads, while an entry-level refusal (unknown charger, wrong
 * version, non-administrator) arrives as a rejected message and is handled as transport failure.
 */
export function decodeSettingsAnswer(raw: unknown): SettingsDecodeResult {
  try {
    if (!isRecord(raw)) {
      return malformed();
    }
    const version = raw["api_version"];
    if (typeof version === "number" && version !== SETTINGS_API_VERSION) {
      return { ok: false, failure: "unsupported" };
    }
    exactKeys(raw, ENVELOPE_KEYS);
    if (version !== SETTINGS_API_VERSION) {
      return malformed();
    }
    const ok = booleanValue(raw, "ok");
    const error = textOrNull(raw, "error");
    const rawSettings = raw["settings"];
    const rawPause = raw["pause"];
    const settings = rawSettings === null ? null : decodeSettingsRecord(rawSettings);
    const pause = rawPause === null ? null : decodePauseObservation(rawPause);
    if (ok) {
      if (error !== null || settings === null || pause === null) {
        return malformed();
      }
      return { ok: true, value: { ok: true, settings, pause } };
    }
    if (error === null || error === "" || (settings === null) !== (pause === null)) {
      return malformed();
    }
    return { ok: true, value: { ok: false, code: error, settings, pause } };
  } catch (error) {
    if (error instanceof MalformedPayload) {
      return malformed();
    }
    throw error;
  }
}


export type SettingsEditorKind = "energy" | "deadline" | "current" | "plan";

/**
 * The editors this contract covers. `plan` is the everyday popover: requested energy, finish by
 * (deadline toggle, time, charging-periods slider) and current, saved as one replacement. `energy`,
 * `deadline` and `current` are those same fields as focused edits.
 */
export const SETTINGS_EDITOR_KINDS = [
  "plan",
  "energy",
  "deadline",
  "current",
] as const satisfies readonly SettingsEditorKind[];

/**
 * The full replacement body for an accepted record: eleven keys, no `revision`.
 *
 * Built key by key with fresh nested objects so a save cannot reach back into the decoded record.
 * The revision travels beside the body as `expected_revision`.
 */
export function encodeBody(record: SettingsRecord): SettingsBody {
  return {
    area_id: record.area_id,
    overrides: record.overrides.map((item) => ({
      area_id: item.area_id,
      vat: { ...item.vat },
      tax: { ...item.tax },
      transfer: { ...item.transfer },
    })),
    phases: record.phases,
    amps: record.amps,
    requested_kwh: record.requested_kwh,
    max_periods: record.max_periods,
    departure_enabled: record.departure_enabled,
    departure_time: record.departure_time,
    strategy: record.strategy,
    driver: record.driver,
    target: { ...record.target },
  };
}

/**
 * What choosing an available, non-selected strategy row would send: the accepted record unchanged
 * except `strategy`. Every other field is copied as `encodeBody` does. `changed` is `false` only
 * when the record was already on that strategy, which the view never asks for.
 */
export function strategyReplacement(record: SettingsRecord, strategy: string): ReplacementCheck {
  if (!(STRATEGIES as readonly string[]).includes(strategy)) {
    return { ok: false, errorKey: "settings.error.invalid" };
  }
  return { ok: true, body: { ...encodeBody(record), strategy }, changed: strategy !== record.strategy };
}

export interface SettingsFormValues {
  energy: string;
  deadlineEnabled: boolean;
  deadlineTime: string;
  maxPeriods: string;
  current: string;
  driver: string;
  targetPercent: string;
  vehicleId: string;
  /** `"1"`, `"3"`, or `""` while the record has no phase count. Written by the Plan dialog only. */
  phases: string;
}

export function formFromRecord(record: SettingsRecord): SettingsFormValues {
  return {
    energy: String(record.requested_kwh),
    deadlineEnabled: record.departure_enabled,
    deadlineTime: record.departure_time,
    maxPeriods: String(record.max_periods),
    current: record.amps === null ? "" : String(record.amps),
    driver: record.driver,
    targetPercent:
      record.target.target_percent === null ? "" : String(record.target.target_percent),
    vehicleId: record.target.vehicle_id ?? "",
    phases: record.phases === null ? "" : String(record.phases),
  };
}

export type FormCheck<T> =
  | { ok: true; value: T }
  | { ok: false; errorKey: TranslationKey };

/**
 * A decimal number as a `type="number"` input reports it. `Number("")` and `Number(" ")` are `0`,
 * so an empty field is judged before conversion; a comma is accepted. Non-finite values and values
 * outside the field's range are refused.
 */
function decimal(text: string, minimum: number, maximum: number): FormCheck<number> {
  const trimmed = text.trim().replace(",", ".");
  if (trimmed === "") {
    return { ok: false, errorKey: "settings.error.required" };
  }
  const value = Number(trimmed);
  if (!Number.isFinite(value)) {
    return { ok: false, errorKey: "settings.error.invalidNumber" };
  }
  if (value < minimum || value > maximum) {
    return { ok: false, errorKey: "settings.error.outOfRange" };
  }
  return { ok: true, value };
}

function integer(text: string, minimum: number, maximum: number): FormCheck<number> {
  const trimmed = text.trim();
  if (trimmed === "") {
    return { ok: false, errorKey: "settings.error.required" };
  }
  const value = Number(trimmed);
  if (!Number.isFinite(value) || !Number.isInteger(value)) {
    return { ok: false, errorKey: "settings.error.invalidNumber" };
  }
  if (value < minimum || value > maximum) {
    return { ok: false, errorKey: "settings.error.outOfRange" };
  }
  return { ok: true, value };
}

export const TARGET_PERCENT_MIN = 0;
export const TARGET_PERCENT_MAX = 100;
export const CAPACITY_MIN_KWH = 1;
export const CAPACITY_MAX_KWH = 500;

export function checkTargetPercent(text: string): FormCheck<number> {
  return decimal(text, TARGET_PERCENT_MIN, TARGET_PERCENT_MAX);
}

export function checkCapacity(text: string): FormCheck<number> {
  const check = decimal(text, CAPACITY_MIN_KWH, CAPACITY_MAX_KWH);
  return check.ok ? { ok: true, value: Math.round(check.value * 10) / 10 } : check;
}

export function checkEnergy(text: string): FormCheck<number> {
  return decimal(text, ENERGY_MIN_KWH, ENERGY_MAX_KWH);
}

export function checkDeadlineTime(text: string): FormCheck<string> {
  const trimmed = text.trim();
  if (trimmed === "") {
    return { ok: false, errorKey: "settings.error.required" };
  }
  return WALL_TIME.test(trimmed)
    ? { ok: true, value: trimmed }
    : { ok: false, errorKey: "settings.error.invalidTime" };
}

export function checkMaxPeriods(text: string): FormCheck<number> {
  return integer(text, PERIODS_MIN, PERIODS_MAX);
}

export function checkCurrent(text: string): FormCheck<number> {
  return integer(text, AMPS_MIN, AMPS_MAX);
}

export interface CurrentRange {
  minA: number;
  maxA: number;
}

/**
 * The planned current against the charger's own range, for a value the reader changed.
 *
 * The contract's 1 to 80 A bound is judged first (`checkCurrent`), then the charger's range, so the
 * card refuses what the backend would (`invalid_amps`). A stored value outside the range is never
 * re-judged: leaving it alone is not a change (`replacementFor` only asks when the value moved).
 */
export function checkCurrentInRange(text: string, range: CurrentRange): FormCheck<number> {
  const amps = checkCurrent(text);
  if (!amps.ok) {
    return amps;
  }
  return amps.value < range.minA || amps.value > range.maxA
    ? { ok: false, errorKey: "settings.error.outOfRange" }
    : amps;
}

export const NOMINAL_VOLTS_SINGLE_PHASE = 230;
export const NOMINAL_VOLTS_THREE_PHASE = 400;

/**
 * The nominal power a connector draws at a current, in kW, for a known phase count.
 *
 * Mirrors `planner.power_kw` term for term: 230 V on one phase, or 400 V three-phase with line
 * current `sqrt(3)` times the phase current. This is not interchangeable with a `230 x 3` shortcut
 * (at 16 A: 11.085 kW against 11.04 kW). Parity is pinned by the same literals in the TypeScript test
 * and `tests/test_planner.py`.
 *
 * `null` means no power can be named: the phase count is unknown or not one this release knows.
 */
export function nominalPowerKw(amps: number, phases: number | null): number | null {
  if (!Number.isFinite(amps)) {
    return null;
  }
  if (phases === 1) {
    return (NOMINAL_VOLTS_SINGLE_PHASE * amps) / 1000;
  }
  if (phases === 3) {
    return (Math.sqrt(3) * NOMINAL_VOLTS_THREE_PHASE * amps) / 1000;
  }
  return null;
}

/**
 * The ordinary slider interval for energy, and the current slider's whole step.
 *
 * The energy slider is a half-kWh grid between 0.5 and 100 kWh; the current slider spans the
 * contract's range in whole amperes. Both are conveniences beside an exact field, never a limit on
 * what can be stored.
 *
 * Not the backend's `planner.ENERGY_SLIDER_MIN_KWH`/`_MAX_KWH`: those bound a different control (the
 * target-SoC slider) and must not be unified with these.
 */
export const ENERGY_SLIDER_MIN_KWH = 0.5;
export const ENERGY_SLIDER_MAX_KWH = 100;
export const ENERGY_SLIDER_STEP_KWH = 0.5;
export const CURRENT_SLIDER_STEP_A = 1;

const STEP_EPSILON = 1e-9;

/**
 * Whether a slider with this step can represent an exact value.
 *
 * HTML anchors the step grid at the slider's minimum, so a half-kWh slider cannot show `20.25`. A
 * representable value is drawn on the slider; otherwise the slider is disabled and marked while the
 * exact number field keeps the value. Nothing is clamped or rounded.
 */
export function sliderRepresents(value: number, minimum: number, step: number): boolean {
  if (!Number.isFinite(value) || value < minimum) {
    return false;
  }
  const steps = (value - minimum) / step;
  return Math.abs(steps - Math.round(steps)) < STEP_EPSILON;
}

/**
 * The energy slider's domain for an exact value: the ordinary interval, extended upward to include a
 * representable value above it, so a stored `150` is drawn where it is.
 */
export function energySliderMaximum(value: number): number {
  return Number.isFinite(value) && value > ENERGY_SLIDER_MAX_KWH ? value : ENERGY_SLIDER_MAX_KWH;
}

/**
 * The number field's energy bounds: min 0.1, max 1000, any finite decimal between them.
 *
 * A `step` of 0.5 anchored at 0.1 would make an ordinary `20.5` browser-invalid, so the input
 * accepts any decimal and the application validates the range. The half-kWh convenience lives on
 * the slider (`ENERGY_SLIDER_STEP_KWH`).
 */
export const ENERGY_MIN_KWH = 0.1;
export const ENERGY_MAX_KWH = 1000;
export const PERIODS_MIN = 1;
export const PERIODS_MAX = 8;
export const AMPS_MIN = 1;
export const AMPS_MAX = 80;
export const CONSUMPTION_MIN_KWH_PER_10KM = 0.1;
export const CONSUMPTION_MAX_KWH_PER_10KM = 50;

export function checkConsumption(text: string): FormCheck<number> {
  return decimal(text, CONSUMPTION_MIN_KWH_PER_10KM, CONSUMPTION_MAX_KWH_PER_10KM);
}
/**
 * What one focused Save would send: the whole replacement, or the input that stops it.
 *
 * `changed` is the zero-request rule: a Save whose owned fields equal the accepted record sends
 * nothing, since an unchanged replacement still costs a revision and a reconcile.
 */
export type ReplacementCheck =
  | { ok: true; body: SettingsBody; changed: boolean }
  | { ok: false; errorKey: TranslationKey };

export function replacementFor(
  kind: SettingsEditorKind,
  record: SettingsRecord,
  values: SettingsFormValues,
  range: CurrentRange | null = null,
  /**
   * The record the reader saw when the editor opened, given only when a Save is reapplied onto a
   * newer record after a conflict. Then only the fields the reader moved are written, so another
   * client's concurrent change is kept.
   */
  opened: SettingsRecord | null = null,
): ReplacementCheck {
  const body = encodeBody(record);
  const energy = kind === "energy" || kind === "plan" ? checkEnergy(values.energy) : null;
  if (energy !== null && !energy.ok) {
    return energy;
  }
  const amps = kind === "current" || kind === "plan" ? currentCheck(values, record, range) : null;
  if (amps !== null && !amps.ok) {
    return amps;
  }
  const time = kind === "deadline" || kind === "plan" ? checkDeadlineTime(values.deadlineTime) : null;
  if (time !== null && !time.ok) {
    return time;
  }
  const periods = kind === "deadline" || kind === "plan" ? checkMaxPeriods(values.maxPeriods) : null;
  if (periods !== null && !periods.ok) {
    return periods;
  }
  // Mode switch: the driver, and on the target its percentage and vehicle. A field the form did not
  // move is not judged, so a stored out-of-bounds figure never blocks a Save of something else.
  const driverOk = values.driver === "manual_kwh" || values.driver === SETTINGS_DRIVER_TARGET_SOC;
  if (kind === "plan" && !driverOk) {
    return { ok: false, errorKey: "settings.error.invalid" };
  }
  const soc = kind === "plan" && values.driver === SETTINGS_DRIVER_TARGET_SOC;
  const targetPercent = soc ? checkTargetPercent(values.targetPercent) : null;
  if (targetPercent !== null && !targetPercent.ok) {
    return targetPercent;
  }
  let changed = false;
  const next = { ...body };
  if (kind === "plan" && driverOk && (opened === null || values.driver !== opened.driver)) {
    next.driver = values.driver;
    changed = changed || values.driver !== record.driver;
  }
  const target = { ...record.target };
  let targetMoved = false;
  if (
    targetPercent !== null &&
    targetPercent.ok &&
    (opened === null || targetPercent.value !== opened.target.target_percent)
  ) {
    target.target_percent = targetPercent.value;
    targetMoved = targetMoved || targetPercent.value !== record.target.target_percent;
  }
  // The vehicle is written only in target mode: the resolved (or picked) one. Reapplied onto a newer
  // record, only when the reader's choice differs from what they opened.
  const vehicleId = values.vehicleId.trim();
  if (
    soc &&
    vehicleId !== "" &&
    (opened === null || vehicleId !== (opened.target.vehicle_id ?? ""))
  ) {
    target.vehicle_id = vehicleId;
    targetMoved = targetMoved || vehicleId !== record.target.vehicle_id;
  }
  if (targetMoved) {
    next.target = target;
    changed = true;
  }
  // Phases belong to the Plan dialog: a chosen count replaces the stored one (reapplied onto a newer record,
  // only when the reader moved it).
  const chosenPhases = values.phases === "1" ? 1 : values.phases === "3" ? 3 : null;
  if (
    kind === "plan" &&
    chosenPhases !== null &&
    (opened === null || chosenPhases !== opened.phases)
  ) {
    next.phases = chosenPhases;
    changed = changed || chosenPhases !== record.phases;
  }
  if (energy !== null && energy.ok && (opened === null || energy.value !== opened.requested_kwh)) {
    next.requested_kwh = energy.value;
    changed = changed || energy.value !== record.requested_kwh;
  }
  if (amps !== null && amps.ok && (opened === null || amps.value !== opened.amps)) {
    next.amps = amps.value;
    changed = changed || amps.value !== record.amps;
  }
  const deadlineMoved =
    opened === null ||
    (time !== null &&
      time.ok &&
      periods !== null &&
      periods.ok &&
      (values.deadlineEnabled !== opened.departure_enabled ||
        time.value !== opened.departure_time ||
        periods.value !== opened.max_periods));
  if (time !== null && time.ok && periods !== null && periods.ok && deadlineMoved) {
    next.departure_enabled = values.deadlineEnabled;
    next.departure_time = time.value;
    next.max_periods = periods.value;
    changed =
      changed ||
      values.deadlineEnabled !== record.departure_enabled ||
      time.value !== record.departure_time ||
      periods.value !== record.max_periods;
  }
  return { ok: true, body: next, changed };
}

/** The current field, judged against the charger's range only when the reader moved it. */
function currentCheck(
  values: SettingsFormValues,
  record: SettingsRecord,
  range: CurrentRange | null,
): FormCheck<number> {
  const amps = checkCurrent(values.current);
  if (!amps.ok || range === null || amps.value === record.amps) {
    return amps;
  }
  return checkCurrentInRange(values.current, range);
}


export interface SettingsSummaries {
  energy: string;
  deadline: string;
  current: string;
}

/**
 * The compact row's values, from the dashboard's reduced settings section.
 *
 * That section is a summary for labelling the resting triggers, never a source for editing (it is
 * nullable and reduced). Values it cannot state show as "not set". The energy figure is the stored
 * amount, spelled by `energyAmount` so row and dialog agree.
 */
export function settingsSummaries(
  language: Language,
  settings: SettingsRecord | null,
): SettingsSummaries {
  const energy = settings?.requested_kwh ?? null;
  const amps = settings?.amps ?? null;
  const time = settings?.departure_time ?? null;
  return {
    energy:
      energy === null || !Number.isFinite(energy)
        ? translate(language, "settings.energy.unset")
        : energyAmount(language, energy),
    deadline:
      settings?.departure_enabled === true && time !== null && WALL_TIME.test(time)
        ? time
        : translate(language, "settings.deadline.none"),
    current:
      amps === null || !Number.isFinite(amps)
        ? translate(language, "settings.current.unset")
        : `${formatNumber(language, amps, 0)} A`,
  };
}

/**
 * The Plan cell's one line, e.g. `20 kWh · No deadline · 16 A`: the three planning values in the
 * popover's order, from the reduced dashboard section.
 */
export function planSummaryParts(language: Language, settings: SettingsRecord | null): string[] {
  const summaries = settingsSummaries(language, settings);
  // A target-SoC charger leads with the target (`80 %`), a manual one with its energy (`20 kWh`).
  const first =
    settings?.driver === SETTINGS_DRIVER_TARGET_SOC
      ? settings.target.target_percent === null || !Number.isFinite(settings.target.target_percent)
        ? translate(language, "settings.energy.unset")
        : percentAmount(language, settings.target.target_percent)
      : summaries.energy;
  return [first, summaries.deadline, summaries.current];
}

export function manualEnergyReadOnly(record: SettingsRecord): boolean {
  return record.driver === SETTINGS_DRIVER_TARGET_SOC;
}

/**
 * The sentence for a settings code, chosen once in the settings language.
 *
 * A refusal's prose never reaches this card, so the code is the whole content and this mapping the
 * whole wording. Kept apart: a write that never committed, one that committed but could not be
 * reconciled, an invalid body, a version disagreement, and an unreachable charger. An unknown code
 * gets one generic sentence.
 */
export function settingsErrorKey(code: string | null): TranslationKey {
  if (code === null) {
    return "settings.error.generic";
  }
  if (code === SETTINGS_NOT_COMMITTED) {
    return "settings.error.notCommitted";
  }
  if (code === SETTINGS_RECONCILE_FAILED) {
    return "settings.error.reconcileFailed";
  }
  if (code === "spotnav_settings_unavailable") {
    return "settings.error.unavailable";
  }
  if (code.startsWith("invalid_")) {
    return "settings.error.invalid";
  }
  if (code === "unauthorized") {
    return "settings.error.readOnly";
  }
  if (code === "spotnav_unsupported_api_version") {
    return "settings.error.version";
  }
  if (code.endsWith("charger") || code === "spotnav_charger_unloaded") {
    return "settings.error.charger";
  }
  return "settings.error.generic";
}


export interface ValueRow {
  key: string;
  label: string;
  value: string;
}

/**
 * VAT, energy tax and grid fee as the plan applies them: the figure with its unit, "off", or "not
 * set". Read from the dashboard's `fiscal` section, so it costs no request and cannot disagree with
 * the plan.
 */
export function fiscalRows(language: Language, fiscal: Fiscal | null): ValueRow[] {
  if (fiscal === null) {
    return [];
  }
  const labels = {
    vat: translate(language, "settings.fiscal.vat"),
    tax: translate(language, "settings.fiscal.tax"),
    transfer: translate(language, "settings.fiscal.transfer"),
  } as const;
  return (["vat", "tax", "transfer"] as const).map((key) => {
    const component = fiscal[key];
    let value: string;
    if (component.policy === "off") {
      value = translate(language, "settings.value.off");
    } else if (component.effective === null) {
      value = translate(language, "settings.value.unset");
    } else {
      const figure = formatNumber(language, component.effective, 2);
      value = component.unit === null ? figure : `${figure} ${component.unit}`;
    }
    return { key, label: labels[key], value };
  });
}
