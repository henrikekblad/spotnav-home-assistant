// Client side of `spotnav/get_entity_config` and `spotnav/update_entity_config` (api_version 1,
// administrators only): one decoding boundary and the pure compare-and-set body a save sends.
// Both answer with `{api_version, ok, error, field_errors, config}`, and `config` travels with every
// outcome that reached a charger, so the card converges from the answer. No backend or DOM access
// here.
//
// The backend lists only the active measurement mode's phase fields, so the other mode's are
// described here (`phaseFields`) with the backend's own classes; the server validates regardless.

import type { TranslationKey } from "./i18n";
import { ENTITY_CONFIG_API_VERSION } from "./types";
import { decodeVehicle as decodeVehicleRow, type Vehicle } from "./validate";

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
    bad();
  }
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) {
      bad();
    }
  }
}

function text(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" && value !== "" ? value : bad();
}

function textOrNull(source: Record<string, unknown>, key: string): string | null {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "string" ? value : bad();
}

function flag(source: Record<string, unknown>, key: string): boolean {
  const value = source[key];
  return typeof value === "boolean" ? value : bad();
}

function textList(source: Record<string, unknown>, key: string): string[] {
  const value = source[key];
  if (!Array.isArray(value)) {
    return bad();
  }
  return value.map((entry) => (typeof entry === "string" ? entry : bad()));
}

function numberOrNull(source: Record<string, unknown>, key: string): number | null {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "number" && Number.isFinite(value) ? value : bad();
}

export const ENTITY_NOT_ADMIN = "spotnav_not_admin";
export const ENTITY_CONFLICT = "spotnav_conflict";
export const ENTITY_INVALID_VALUE = "spotnav_invalid_value";
export const ENTITY_NO_SITE = "spotnav_no_site";

export type EntityScope = "charger" | "site";

export const MEASUREMENT_DIRECT = "direct_phase_current";
export const MEASUREMENT_DERIVED = "derived_phase_current";

export interface EntityRef {
  entityId: string;
  friendlyName: string;
}

export interface StoredEntityRef extends EntityRef {
  exists: boolean;
}

export interface EntityEffective extends EntityRef {
  source: "configured" | "automatic";
}

interface FieldBase {
  field: string;
  scope: EntityScope;
  required: boolean;
  writable: boolean;
}

export interface EntityFieldEntity extends FieldBase {
  kind: "entity";
  current: StoredEntityRef | null;
  effective: EntityEffective | null;
  /**
   * Only the charger's current limit: whether "None" (SpotNav sets no current) may be chosen, whether it
   * is what is stored, and what Automatic finds (also while None is chosen). Absent on every other field.
   */
  none: { allowed: boolean; chosen: boolean; automatic: EntityRef | null } | null;
  domains: string[];
  deviceClasses: string[];
}

export interface EntityFieldNumber extends FieldBase {
  kind: "number";
  minimum: number;
  value: number | null;
}

export interface EntityFieldEnum extends FieldBase {
  kind: "enum";
  choices: string[];
  value: string | null;
}

export interface EntityFieldFlag extends FieldBase {
  kind: "flag";
  value: boolean;
}

export type EntityField = EntityFieldEntity | EntityFieldNumber | EntityFieldEnum | EntityFieldFlag;

/** How each phase's current was obtained (`site_capacity.CurrentBasis`); `null` for an unusable phase. */
export type CurrentBasis = "measured" | "apparent" | "reactive" | "estimated";

/**
 * The stored source a direct site reads its phase currents from in place of the three `direct_L{n}`
 * entities: one entity carrying each phase as an attribute, or an entity per phase. Read-only;
 * naming the three entities replaces it.
 */
export type SiteCurrentSource =
  | { kind: "attributes"; entityId: string; name: string; attributes: Record<(typeof PHASES)[number], string | null> }
  | { kind: "separate_entities"; entityIds: Record<(typeof PHASES)[number], string | null> };

export interface SiteMeasurement {
  mode: string;
  currentEstimated: boolean;
  assumedPowerFactor: number | null;
  basis: Record<(typeof PHASES)[number], CurrentBasis | null>;
  currentSource: SiteCurrentSource | null;
}

/** One phase that makes the site's measurement unusable: it read nothing usable, or is stale. */
export interface SiteWarningPhase {
  phase: string;
  cause: "no_value" | "stale";
  entityId: string | null;
  ageS: number | null;
}

export interface SiteWarning {
  code: string;
  integration: string | null;
  entityId: string | null;
  intervalS: number | null;
  option: string | null;
  deviceName: string | null;
  phases: SiteWarningPhase[];
  /** `battery_import_limit_differs` only: the battery's grid import limit and SpotNav's, in A per phase. */
  limitsA: { battery: number; spotnav: number } | null;
  /** `measurement_unhealthy` only: the meter's sensors that are unavailable together, else empty. */
  unavailableEntities: string[];
  /** They are a known inverter integration's: it may be in standby. */
  inverter: boolean;
  /** `measurement_unhealthy` only: the phases that read a negative current on a site that reads it unsigned. */
  negativePhases: string[];
}

export interface DetectedEntityRow {
  role: string;
  phase: string | null;
  entityId: string;
  friendlyName: string;
  disabled: boolean;
}

export interface DetectedMeter {
  id: string;
  integration: string;
  title: string;
  mode: string;
  confidence: "high" | "medium" | "low";
  currentSigned: boolean;
  powerInverted: boolean;
  estimated: boolean;
  disabledEntities: string[];
  entities: DetectedEntityRow[];
  warnings: string[];
  applied: boolean;
}

export interface DetectedBattery {
  id: string;
  integration: string;
  title: string;
  entityId: string;
  friendlyName: string;
  inverted: boolean;
  dischargeEntityId: string | null;
  disabledEntities: string[];
}

export interface EntitySite {
  name: string;
  chargerCount: number;
  measurement: SiteMeasurement;
  warnings: SiteWarning[];
  meters: DetectedMeter[];
  batteries: DetectedBattery[];
}

/**
 * One vehicle whose charge-level sensor can be chosen (`spotnav/choose_vehicle_soc`): `selected` is
 * the sensor in use (`null` when none chosen), `source` says whether a person chose it
 * (`confirmed`) or it is the only candidate (`automatic`), `candidates` are the sensors to pick from.
 */
export interface VehicleSoc {
  id: string;
  name: string;
  selected: EntityRef | null;
  source: "confirmed" | "automatic" | null;
  candidates: EntityRef[];
}

/** How a charger is started and stopped, as its adapter describes it. */
export interface ControlStartStop {
  kind: "switch" | "select" | "buttons" | "easee" | "number_pause" | "other";
  entityIds: string[];
  inverted: boolean;
  startOption: string | null;
  stopOption: string | null;
}

/** How a current is set, if it is. `enabled` is the person's opt-in. */
export interface ControlCurrent {
  kind: "none" | "ocpp" | "number" | "service";
  entityId: string | null;
  service: string | null;
  enabled: boolean;
}

/** The write policy every current write obeys (`execution/charger_profiles.py`). */
export interface ControlPolicy {
  minIntervalS: number;
  maxWritesPerMinute: number | null;
  flashStored: boolean;
  regulatorWrites: boolean;
  zeroPauses: boolean;
  ignoredWhilePaused: boolean;
  installationWide: boolean;
  resendAfterPlugIn: boolean;
}

export interface ControlCapabilities {
  startStop: boolean;
  setCurrent: boolean;
  regulatedCurrent: boolean;
  readsChargingState: boolean;
  readsMeasuredCurrent: boolean;
  readsEnergyRegister: boolean;
}

/** `own_mode`: one of the charger's own controllers is on and can fight SpotNav; `disabled`: its own enable
 * switch is off, so SpotNav cannot start it; `duplicate_charger`: another SpotNav entry (named in `state`) is
 * the same physical charger, sharing what `label` says (`entity_id` holds the shared identifier);
 * `other_controller`: another integration (named in `label`) that switches or limits chargers is set up and
 * fights SpotNav, for this charger (`state` is `charger`) or for the installation (`installation`). */
export type ControlConflictKind = "own_mode" | "disabled" | "duplicate_charger" | "other_controller";

export interface ControlConflict {
  kind: ControlConflictKind;
  entityId: string;
  label: string;
  state: string;
}

/** The chosen control path and its write policy. Read only: the card shows it, it cannot edit it. */
export interface EntityControl {
  platform: string | null;
  startStop: ControlStartStop;
  current: ControlCurrent;
  chargingState: { source: "status" | "control"; entityId: string | null };
  policy: ControlPolicy;
  capabilities: ControlCapabilities;
  conflicts: ControlConflict[];
}

export interface EntityConfig {
  chargerId: string;
  fields: EntityField[];
  site: EntitySite | null;
  vehicles: VehicleSoc[];
  /** `null` while the charger is not loaded. */
  control: EntityControl | null;
}

export interface EntityFieldError {
  field: string;
  code: string;
}

export type EntityAnswer =
  | { ok: true; config: EntityConfig }
  | { ok: false; code: string; config: EntityConfig | null; fieldErrors: EntityFieldError[] };

export type EntityDecodeResult =
  | { ok: true; value: EntityAnswer }
  | { ok: false; failure: "unsupported" | "malformed" };

const ENVELOPE_KEYS = ["api_version", "ok", "error", "field_errors", "config"] as const;
const ENTITY_KEYS = [
  "allowed_device_classes",
  "allowed_domains",
  "current",
  "effective",
  "field",
  "kind",
  "required",
  "scope",
  "writable",
] as const;
/** Present on the current limit alone, so an older backend (and every other field) still decodes. */
const ENTITY_OPTIONAL_KEYS = ["none"] as const;
const NUMBER_KEYS = ["field", "kind", "minimum", "required", "scope", "value", "writable"] as const;
const ENUM_KEYS = ["choices", "field", "kind", "required", "scope", "value", "writable"] as const;
const FLAG_KEYS = ["field", "kind", "required", "scope", "value", "writable"] as const;

function decodeField(raw: unknown): EntityField {
  const source = record(raw);
  const kind = source["kind"];
  const scopeValue = source["scope"];
  if (scopeValue !== "charger" && scopeValue !== "site") {
    return bad();
  }
  const scope: EntityScope = scopeValue;
  const base = {
    field: text(source, "field"),
    scope,
    required: flag(source, "required"),
    writable: flag(source, "writable"),
  };
  if (kind === "entity") {
    const rawNone = source["none"];
    exactKeys(
      source,
      rawNone === undefined ? ENTITY_KEYS : [...ENTITY_KEYS, ...ENTITY_OPTIONAL_KEYS],
    );
    let none: EntityFieldEntity["none"] = null;
    if (rawNone !== undefined) {
      const choice = record(rawNone);
      exactKeys(choice, ["allowed", "automatic", "chosen"]);
      const rawAutomatic = choice["automatic"];
      let automatic: EntityRef | null = null;
      if (rawAutomatic !== null) {
        const ref = record(rawAutomatic);
        exactKeys(ref, ["entity_id", "friendly_name"]);
        automatic = { entityId: text(ref, "entity_id"), friendlyName: text(ref, "friendly_name") };
      }
      none = { allowed: flag(choice, "allowed"), chosen: flag(choice, "chosen"), automatic };
    }
    const rawCurrent = source["current"];
    let current: StoredEntityRef | null = null;
    if (rawCurrent !== null) {
      const ref = record(rawCurrent);
      exactKeys(ref, ["entity_id", "friendly_name", "exists"]);
      current = {
        entityId: text(ref, "entity_id"),
        friendlyName: text(ref, "friendly_name"),
        exists: flag(ref, "exists"),
      };
    }
    const rawEffective = source["effective"];
    let effective: EntityEffective | null = null;
    if (rawEffective !== null) {
      const ref = record(rawEffective);
      exactKeys(ref, ["entity_id", "friendly_name", "source"]);
      const origin = ref["source"];
      if (origin !== "configured" && origin !== "automatic") {
        return bad();
      }
      effective = {
        entityId: text(ref, "entity_id"),
        friendlyName: text(ref, "friendly_name"),
        source: origin,
      };
    }
    return {
      ...base,
      kind,
      current,
      effective,
      none,
      domains: textList(source, "allowed_domains"),
      deviceClasses: textList(source, "allowed_device_classes"),
    };
  }
  if (kind === "number") {
    exactKeys(source, NUMBER_KEYS);
    const minimum = source["minimum"];
    if (typeof minimum !== "number" || !Number.isFinite(minimum)) {
      return bad();
    }
    return { ...base, kind, minimum, value: numberOrNull(source, "value") };
  }
  if (kind === "enum") {
    exactKeys(source, ENUM_KEYS);
    return { ...base, kind, choices: textList(source, "choices"), value: textOrNull(source, "value") };
  }
  if (kind === "flag") {
    exactKeys(source, FLAG_KEYS);
    return { ...base, kind, value: flag(source, "value") };
  }
  return bad();
}

function list(source: Record<string, unknown>, key: string): unknown[] {
  const value = source[key];
  return Array.isArray(value) ? value : bad();
}

function intervalOrNull(source: Record<string, unknown>, key: string): number | null {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "number" && Number.isFinite(value) ? value : bad();
}

const BASES: readonly string[] = ["measured", "apparent", "reactive", "estimated"];

function decodeBasis(value: unknown): CurrentBasis | null {
  if (value === null) {
    return null;
  }
  return typeof value === "string" && BASES.includes(value) ? (value as CurrentBasis) : bad();
}

function decodePhaseTexts(raw: unknown): Record<(typeof PHASES)[number], string | null> {
  const source = record(raw);
  exactKeys(source, PHASES);
  return { L1: textOrNull(source, "L1"), L2: textOrNull(source, "L2"), L3: textOrNull(source, "L3") };
}

function decodeCurrentSource(raw: unknown): SiteCurrentSource | null {
  if (raw === null) {
    return null;
  }
  const source = record(raw);
  exactKeys(source, ["kind", "entity_id", "name", "attributes", "entity_ids"]);
  const kind = oneOf(source, "kind", ["attributes", "separate_entities"] as const);
  if (kind === "attributes") {
    return {
      kind,
      entityId: text(source, "entity_id"),
      name: text(source, "name"),
      attributes: decodePhaseTexts(source["attributes"]),
    };
  }
  return { kind, entityIds: decodePhaseTexts(source["entity_ids"]) };
}

function decodeMeasurement(raw: unknown): SiteMeasurement {
  const source = record(raw);
  exactKeys(source, ["mode", "current_estimated", "assumed_power_factor", "basis", "current_source"]);
  const basis = record(source["basis"]);
  exactKeys(basis, PHASES);
  return {
    mode: text(source, "mode"),
    currentEstimated: flag(source, "current_estimated"),
    assumedPowerFactor: numberOrNull(source, "assumed_power_factor"),
    basis: {
      L1: decodeBasis(basis["L1"]),
      L2: decodeBasis(basis["L2"]),
      L3: decodeBasis(basis["L3"]),
    },
    currentSource: decodeCurrentSource(source["current_source"]),
  };
}

function decodeWarningPhase(raw: unknown): SiteWarningPhase {
  const source = record(raw);
  exactKeys(source, ["phase", "cause", "entity_id", "age_s"]);
  return {
    phase: text(source, "phase"),
    cause: oneOf(source, "cause", ["no_value", "stale"] as const),
    entityId: textOrNull(source, "entity_id"),
    ageS: intervalOrNull(source, "age_s"),
  };
}

function decodeWarning(raw: unknown): SiteWarning {
  const source = record(raw);
  exactKeys(source, [
    "code",
    "integration",
    "entity_id",
    "interval_s",
    "option",
    "device_name",
    "phases",
    "limits_a",
    "unavailable_entities",
    "inverter",
    "negative_phases",
  ]);
  const unavailable = source["unavailable_entities"];
  if (!Array.isArray(unavailable) || !unavailable.every((entry) => typeof entry === "string")) {
    return bad();
  }
  const negative = source["negative_phases"];
  if (!Array.isArray(negative) || !negative.every((entry) => typeof entry === "string")) {
    return bad();
  }
  const phases = source["phases"];
  if (!Array.isArray(phases)) {
    return bad();
  }
  return {
    code: text(source, "code"),
    integration: textOrNull(source, "integration"),
    entityId: textOrNull(source, "entity_id"),
    intervalS: intervalOrNull(source, "interval_s"),
    option: textOrNull(source, "option"),
    deviceName: textOrNull(source, "device_name"),
    phases: phases.map(decodeWarningPhase),
    limitsA: decodeLimits(source["limits_a"]),
    unavailableEntities: unavailable as string[],
    inverter: flag(source, "inverter"),
    negativePhases: negative as string[],
  };
}

function decodeLimits(raw: unknown): { battery: number; spotnav: number } | null {
  if (raw === null) {
    return null;
  }
  const source = record(raw);
  exactKeys(source, ["battery", "spotnav"]);
  const battery = source["battery"];
  const spotnav = source["spotnav"];
  return typeof battery === "number" && Number.isFinite(battery) && typeof spotnav === "number" && Number.isFinite(spotnav)
    ? { battery, spotnav }
    : bad();
}

function decodeDetectedEntity(raw: unknown): DetectedEntityRow {
  const source = record(raw);
  exactKeys(source, ["role", "phase", "entity_id", "friendly_name", "disabled"]);
  return {
    role: text(source, "role"),
    phase: textOrNull(source, "phase"),
    entityId: text(source, "entity_id"),
    friendlyName: text(source, "friendly_name"),
    disabled: flag(source, "disabled"),
  };
}

function decodeMeter(raw: unknown): DetectedMeter {
  const source = record(raw);
  exactKeys(source, [
    "id",
    "integration",
    "title",
    "mode",
    "confidence",
    "current_signed",
    "power_inverted",
    "estimated",
    "disabled_entities",
    "entities",
    "warnings",
    "applied",
  ]);
  const confidence = source["confidence"];
  if (confidence !== "high" && confidence !== "medium" && confidence !== "low") {
    return bad();
  }
  return {
    id: text(source, "id"),
    integration: text(source, "integration"),
    title: text(source, "title"),
    mode: text(source, "mode"),
    confidence,
    currentSigned: flag(source, "current_signed"),
    powerInverted: flag(source, "power_inverted"),
    estimated: flag(source, "estimated"),
    disabledEntities: textList(source, "disabled_entities"),
    entities: list(source, "entities").map(decodeDetectedEntity),
    warnings: textList(source, "warnings"),
    applied: flag(source, "applied"),
  };
}

function decodeBattery(raw: unknown): DetectedBattery {
  const source = record(raw);
  exactKeys(source, [
    "id",
    "integration",
    "title",
    "entity_id",
    "friendly_name",
    "inverted",
    "discharge_entity_id",
    "disabled_entities",
  ]);
  return {
    id: text(source, "id"),
    integration: text(source, "integration"),
    title: text(source, "title"),
    entityId: text(source, "entity_id"),
    friendlyName: text(source, "friendly_name"),
    inverted: flag(source, "inverted"),
    dischargeEntityId: textOrNull(source, "discharge_entity_id"),
    disabledEntities: textList(source, "disabled_entities"),
  };
}

function decodeRef(raw: unknown): EntityRef {
  const ref = record(raw);
  exactKeys(ref, ["entity_id", "friendly_name"]);
  return { entityId: text(ref, "entity_id"), friendlyName: text(ref, "friendly_name") };
}

function decodeVehicle(raw: unknown): VehicleSoc {
  const source = record(raw);
  exactKeys(source, ["id", "name", "selected", "source", "candidates"]);
  const origin = source["source"];
  if (origin !== "confirmed" && origin !== "automatic" && origin !== null) {
    return bad();
  }
  const candidates = source["candidates"];
  if (!Array.isArray(candidates)) {
    return bad();
  }
  return {
    id: text(source, "id"),
    name: text(source, "name"),
    selected: source["selected"] === null ? null : decodeRef(source["selected"]),
    source: origin,
    candidates: candidates.map(decodeRef),
  };
}

function oneOf<T extends string>(source: Record<string, unknown>, key: string, allowed: readonly T[]): T {
  const value = source[key];
  return allowed.find((candidate) => candidate === value) ?? bad();
}

const START_STOP_KINDS = ["switch", "select", "buttons", "easee", "number_pause"] as const;

/** A kind this card knows, or `other` for one a newer backend added: it is worded, never rejected. */
function lenientKind<T extends string>(source: Record<string, unknown>, key: string, allowed: readonly T[]): T | "other" {
  const value = source[key];
  if (typeof value !== "string" || value === "") {
    return bad();
  }
  return allowed.find((candidate) => candidate === value) ?? "other";
}

function integer(source: Record<string, unknown>, key: string): number {
  const value = source[key];
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : bad();
}

function decodeControl(raw: unknown): EntityControl {
  const source = record(raw);
  exactKeys(source, ["platform", "start_stop", "current", "charging_state", "policy", "capabilities", "conflicts"]);
  const startStop = record(source["start_stop"]);
  exactKeys(startStop, ["kind", "entity_ids", "inverted", "start_option", "stop_option"]);
  const current = record(source["current"]);
  exactKeys(current, ["kind", "entity_id", "service", "enabled"]);
  const state = record(source["charging_state"]);
  exactKeys(state, ["source", "entity_id"]);
  const policy = record(source["policy"]);
  exactKeys(policy, [
    "min_interval_s",
    "max_writes_per_minute",
    "flash_stored",
    "regulator_writes",
    "zero_pauses",
    "ignored_while_paused",
    "installation_wide",
    "resend_after_plug_in",
  ]);
  const capabilities = record(source["capabilities"]);
  exactKeys(capabilities, [
    "start_stop",
    "set_current",
    "regulated_current",
    "reads_charging_state",
    "reads_measured_current",
    "reads_energy_register",
  ]);
  const conflicts = source["conflicts"];
  if (!Array.isArray(conflicts)) {
    return bad();
  }
  const minInterval = policy["min_interval_s"];
  if (typeof minInterval !== "number" || !Number.isFinite(minInterval) || minInterval < 0) {
    return bad();
  }
  const perMinute = policy["max_writes_per_minute"];
  if (perMinute !== null) {
    integer(policy, "max_writes_per_minute");
  }
  return {
    platform: textOrNull(source, "platform"),
    startStop: {
      kind: lenientKind(startStop, "kind", START_STOP_KINDS),
      entityIds: textList(startStop, "entity_ids"),
      inverted: flag(startStop, "inverted"),
      startOption: textOrNull(startStop, "start_option"),
      stopOption: textOrNull(startStop, "stop_option"),
    },
    current: {
      kind: oneOf(current, "kind", ["none", "ocpp", "number", "service"] as const),
      entityId: textOrNull(current, "entity_id"),
      service: textOrNull(current, "service"),
      enabled: flag(current, "enabled"),
    },
    chargingState: {
      source: oneOf(state, "source", ["status", "control"] as const),
      entityId: textOrNull(state, "entity_id"),
    },
    policy: {
      minIntervalS: minInterval,
      maxWritesPerMinute: perMinute === null ? null : (perMinute as number),
      flashStored: flag(policy, "flash_stored"),
      regulatorWrites: flag(policy, "regulator_writes"),
      zeroPauses: flag(policy, "zero_pauses"),
      ignoredWhilePaused: flag(policy, "ignored_while_paused"),
      installationWide: flag(policy, "installation_wide"),
      resendAfterPlugIn: flag(policy, "resend_after_plug_in"),
    },
    capabilities: {
      startStop: flag(capabilities, "start_stop"),
      setCurrent: flag(capabilities, "set_current"),
      regulatedCurrent: flag(capabilities, "regulated_current"),
      readsChargingState: flag(capabilities, "reads_charging_state"),
      readsMeasuredCurrent: flag(capabilities, "reads_measured_current"),
      readsEnergyRegister: flag(capabilities, "reads_energy_register"),
    },
    conflicts: conflicts.map((entry): ControlConflict => {
      const item = record(entry);
      exactKeys(item, ["kind", "entity_id", "label", "state"]);
      return {
        kind: oneOf(item, "kind", ["own_mode", "disabled", "duplicate_charger", "other_controller"] as const),
        entityId: text(item, "entity_id"),
        label: text(item, "label"),
        state: text(item, "state"),
      };
    }),
  };
}

function decodeConfig(raw: unknown): EntityConfig {
  const source = record(raw);
  exactKeys(source, ["charger_id", "control", "fields", "site", "vehicles"]);
  const vehicles = source["vehicles"];
  if (!Array.isArray(vehicles)) {
    return bad();
  }
  const fields = source["fields"];
  if (!Array.isArray(fields)) {
    return bad();
  }
  const rawSite = source["site"];
  let site: EntitySite | null = null;
  if (rawSite !== null) {
    const siteSource = record(rawSite);
    exactKeys(siteSource, ["name", "charger_count", "measurement", "warnings", "detection"]);
    const count = siteSource["charger_count"];
    if (typeof count !== "number" || !Number.isInteger(count) || count < 0) {
      return bad();
    }
    const detection = record(siteSource["detection"]);
    exactKeys(detection, ["meters", "batteries"]);
    site = {
      name: text(siteSource, "name"),
      chargerCount: count,
      measurement: decodeMeasurement(siteSource["measurement"]),
      warnings: list(siteSource, "warnings").map(decodeWarning),
      meters: list(detection, "meters").map(decodeMeter),
      batteries: list(detection, "batteries").map(decodeBattery),
    };
  }
  return {
    chargerId: text(source, "charger_id"),
    fields: fields.map(decodeField),
    site,
    vehicles: vehicles.map(decodeVehicle),
    control: source["control"] === null ? null : decodeControl(source["control"]),
  };
}

interface Envelope {
  ok: boolean;
  error: string | null;
  config: EntityConfig | null;
  fieldErrors: EntityFieldError[];
}

function decodeEnvelope(raw: Record<string, unknown>, extra: readonly string[]): Envelope | "unsupported" | "malformed" {
  const version = raw["api_version"];
  if (typeof version === "number" && version !== ENTITY_CONFIG_API_VERSION) {
    return "unsupported";
  }
  exactKeys(raw, [...ENVELOPE_KEYS, ...extra]);
  if (version !== ENTITY_CONFIG_API_VERSION) {
    return "malformed";
  }
  const ok = flag(raw, "ok");
  const error = textOrNull(raw, "error");
  const rawConfig = raw["config"];
  const config = rawConfig === null ? null : decodeConfig(rawConfig);
  const rawErrors = raw["field_errors"];
  if (!Array.isArray(rawErrors)) {
    return "malformed";
  }
  const fieldErrors = rawErrors.map((entry): EntityFieldError => {
    const item = record(entry);
    exactKeys(item, ["field", "code"]);
    return { field: text(item, "field"), code: text(item, "code") };
  });
  if (ok) {
    if (error !== null || config === null || fieldErrors.length > 0) {
      return "malformed";
    }
  } else if (error === null || error === "") {
    return "malformed";
  }
  return { ok, error, config, fieldErrors };
}

/**
 * The boundary for both commands' envelope: `unknown` until accepted, a different `api_version`
 * is `unsupported`, anything else that does not fit is `malformed`. No payload text is carried out.
 */
export function decodeEntityAnswer(raw: unknown): EntityDecodeResult {
  try {
    if (!isRecord(raw)) {
      return { ok: false, failure: "malformed" };
    }
    const envelope = decodeEnvelope(raw, []);
    if (typeof envelope === "string") {
      return { ok: false, failure: envelope };
    }
    if (envelope.ok && envelope.config !== null) {
      return { ok: true, value: { ok: true, config: envelope.config } };
    }
    return {
      ok: true,
      value: { ok: false, code: envelope.error ?? "", config: envelope.config, fieldErrors: envelope.fieldErrors },
    };
  } catch (error) {
    if (error instanceof MalformedPayload || (error instanceof Error && error.message === "malformed")) {
      return { ok: false, failure: "malformed" };
    }
    throw error;
  }
}

export type VehicleAnswer =
  | { ok: true; config: EntityConfig; vehicle: Vehicle; fieldErrors: EntityFieldError[] }
  | {
      ok: false;
      code: string;
      config: EntityConfig | null;
      vehicle: Vehicle | null;
      fieldErrors: EntityFieldError[];
    };

export type VehicleDecodeResult =
  | { ok: true; value: VehicleAnswer }
  | { ok: false; failure: "unsupported" | "malformed" };

export function decodeVehicleAnswer(raw: unknown): VehicleDecodeResult {
  try {
    if (!isRecord(raw)) {
      return { ok: false, failure: "malformed" };
    }
    const envelope = decodeEnvelope(raw, ["vehicle"]);
    if (typeof envelope === "string") {
      return { ok: false, failure: envelope };
    }
    const vehicle = raw["vehicle"] === null ? null : decodeVehicleRow(raw["vehicle"]);
    if (envelope.ok) {
      if (envelope.config === null || vehicle === null) {
        return { ok: false, failure: "malformed" };
      }
      return { ok: true, value: { ok: true, config: envelope.config, vehicle, fieldErrors: [] } };
    }
    return {
      ok: true,
      value: {
        ok: false,
        code: envelope.error ?? "",
        config: envelope.config,
        vehicle,
        fieldErrors: envelope.fieldErrors,
      },
    };
  } catch (error) {
    if (error instanceof MalformedPayload || (error instanceof Error && error.message === "malformed")) {
      return { ok: false, failure: "malformed" };
    }
    throw error;
  }
}


export const PHASES = ["L1", "L2", "L3"] as const;
/**
 * The meter's total grid power, which solar and hybrid read on a site whose phases report current only:
 * one signed sensor, or with the second an import/export pair (the first is then the import).
 */
export const GRID_TOTAL_FIELDS = ["grid_power_source_power", "grid_power_source_power_export"] as const;
/** Derived mode's readings per phase: the first two are required, the rest optional sharpeners. */
export const DERIVED_KINDS = ["power", "voltage", "power_export", "reactive_power", "apparent_power", "current"] as const;
export const DERIVED_REQUIRED_KINDS: readonly string[] = ["power", "voltage"];

export function directFieldName(phase: string): string {
  return `direct_${phase}`;
}

export function derivedFieldName(phase: string, kind: string): string {
  return `derived_${phase}_${kind}`;
}

const PHASE_FIELD = /^(direct_L[123]|derived_L[123]_(power|power_export|reactive_power|apparent_power|current|voltage))$/;

export function isPhaseField(field: string): boolean {
  return PHASE_FIELD.test(field);
}

export function phaseFieldNames(mode: string): string[] {
  if (mode === MEASUREMENT_DERIVED) {
    return PHASES.flatMap((phase) => DERIVED_KINDS.map((kind) => derivedFieldName(phase, kind)));
  }
  return PHASES.map(directFieldName);
}

const DERIVED_CLASSES: Record<string, string> = {
  power: "power",
  power_export: "power",
  reactive_power: "reactive_power",
  apparent_power: "apparent_power",
  current: "current",
  voltage: "voltage",
};

/**
 * A phase-meter field as the picker should offer it: the backend's descriptor when listed,
 * otherwise the rule the backend applies to that mode, with nothing set.
 */
export function phaseField(config: EntityConfig, field: string): EntityFieldEntity {
  const listed = config.fields.find((entry) => entry.field === field);
  if (listed !== undefined && listed.kind === "entity") {
    return listed;
  }
  const derived = /^derived_L[123]_(.+)$/.exec(field);
  const deviceClass = derived === null ? "current" : (DERIVED_CLASSES[derived[1] ?? ""] ?? "power");
  return {
    field,
    kind: "entity",
    scope: "site",
    required: derived === null || DERIVED_REQUIRED_KINDS.includes(derived[1] ?? ""),
    writable: true,
    current: null,
    effective: null,
    none: null,
    domains: ["sensor"],
    deviceClasses: [deviceClass],
  };
}

export function fieldsOf(config: EntityConfig, scope: EntityScope): EntityField[] {
  return config.fields.filter((entry) => entry.scope === scope);
}

export function storedMode(config: EntityConfig): string | null {
  const mode = config.fields.find((entry) => entry.field === "measurement_mode");
  return mode !== undefined && mode.kind === "enum" ? mode.value : null;
}


export type EntityDraft = Record<string, string>;

/** What `current_limit` is sent as to say "None": SpotNav sets no current and looks nothing up. */
export const NONE_VALUE = "none";

function readText(field: EntityField): string {
  if (field.kind === "entity") {
    if (field.current === null && field.none !== null && field.none.chosen) {
      return NONE_VALUE;
    }
    return field.current === null ? "" : field.current.entityId;
  }
  if (field.kind === "number") {
    return field.value === null ? "" : String(field.value);
  }
  if (field.kind === "flag") {
    return field.value ? "true" : "false";
  }
  return field.value ?? "";
}

export function draftFrom(config: EntityConfig, scope: EntityScope): EntityDraft {
  const draft: EntityDraft = {};
  for (const field of fieldsOf(config, scope)) {
    if (field.writable) {
      draft[field.field] = readText(field);
    }
  }
  return draft;
}

export type EntityValue = string | number | boolean;

export interface EntityRequest {
  scope: EntityScope;
  expected: Record<string, EntityValue>;
  changes: Record<string, EntityValue>;
}

/** The draft key that asks the server to apply a detected meter or battery, by id. */
export const APPLY_DETECTION = "apply_detection";

export type EntityChange =
  | { ok: true; changed: false }
  | { ok: true; changed: true; request: EntityRequest }
  | { ok: false; errors: EntityFieldError[] };

function parseNumber(value: string): number | null {
  const trimmed = value.trim().replace(",", ".");
  if (trimmed === "") {
    return null;
  }
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

/**
 * What one group's Save would send, judged against the config it was drawn from. Only changed
 * fields go out, with `expected` holding the value last read, so a moved record is a conflict.
 * Of the phase meters only the resulting mode's count; the other mode's were never read, so they
 * have no `expected`. Numbers are checked only for being a number at least the minimum; every other
 * rule is the backend's.
 */
export function entityChange(config: EntityConfig, scope: EntityScope, draft: EntityDraft): EntityChange {
  const changes: Record<string, EntityValue> = {};
  const expected: Record<string, EntityValue> = {};
  const errors: EntityFieldError[] = [];

  const applied = draft[APPLY_DETECTION];
  if (scope === "site" && applied !== undefined && applied !== "") {
    // A detected setup is applied on its own: the server recomputes the detection and supplies
    // every entity and sign from it.
    return { ok: true, changed: true, request: { scope, expected: {}, changes: { [APPLY_DETECTION]: applied } } };
  }

  let considered: EntityField[] = fieldsOf(config, scope).filter((field) => field.writable && !isPhaseField(field.field));
  if (scope === "site") {
    const mode = draft["measurement_mode"] ?? storedMode(config) ?? MEASUREMENT_DIRECT;
    considered = considered.concat(phaseFieldNames(mode).map((name) => phaseField(config, name)));
  }

  for (const field of considered) {
    const listed = config.fields.find((entry) => entry.field === field.field);
    const read = listed === undefined ? "" : readText(listed);
    const value = draft[field.field] ?? read;
    if (field.kind === "number") {
      const parsed = parseNumber(value);
      if (parsed === null) {
        errors.push({ field: field.field, code: "required" });
        continue;
      }
      if (Number.isNaN(parsed) || parsed < field.minimum) {
        errors.push({ field: field.field, code: "invalid_value" });
        continue;
      }
      const stored = listed !== undefined && listed.kind === "number" ? listed.value : null;
      if (parsed !== stored) {
        changes[field.field] = parsed;
        if (stored !== null) {
          expected[field.field] = stored;
        }
      }
      continue;
    }
    if (field.kind === "flag") {
      if (value !== read && (value === "true" || value === "false")) {
        changes[field.field] = value === "true";
        expected[field.field] = read === "true";
      }
      continue;
    }
    const trimmed = value.trim();
    if (trimmed === read) {
      continue;
    }
    changes[field.field] = trimmed;
    if (listed !== undefined) {
      expected[field.field] = read;
    }
  }

  if (errors.length > 0) {
    return { ok: false, errors };
  }
  if (Object.keys(changes).length === 0) {
    return { ok: true, changed: false };
  }
  return { ok: true, changed: true, request: { scope, expected, changes } };
}


export interface VehicleSocRequest {
  vehicleId: string;
  entityId: string | null;
}

export function vehicleChoice(vehicle: VehicleSoc): string {
  return vehicle.source === "confirmed" && vehicle.selected !== null ? vehicle.selected.entityId : "";
}

/**
 * The requests one Save sends from the draft (`vehicle id -> entity id`, `""` for automatic): one
 * per vehicle whose choice changed, in listed order.
 */
export function vehicleSocChanges(
  vehicles: readonly VehicleSoc[],
  draft: Readonly<Record<string, string>>,
): VehicleSocRequest[] {
  const requests: VehicleSocRequest[] = [];
  for (const vehicle of vehicles) {
    const chosen = draft[vehicle.id] ?? vehicleChoice(vehicle);
    if (chosen !== vehicleChoice(vehicle)) {
      requests.push({ vehicleId: vehicle.id, entityId: chosen === "" ? null : chosen });
    }
  }
  return requests;
}


const FIELD_LABELS: Record<string, TranslationKey> = {
  charge_control: "entity.field.chargeControl",
  current_limit: "entity.field.currentLimit",
  energy_register_entity: "entity.field.energyRegister",
  power_entity: "entity.field.powerEntity",
  vehicle_soc: "entity.field.vehicleSoc",
  main_fuse_a: "entity.field.mainFuse",
  safety_margin_a: "entity.field.safetyMargin",
  measurement_mode: "entity.field.measurementMode",
  voltage_between_phases_v: "entity.field.voltageBetweenPhases",
  charger_phases: "entity.field.chargerPhases",
  charger_priority: "entity.field.chargerPriority",
  battery_aggregate_power_entity: "entity.field.batteryPower",
  battery_discharge_power_entity: "entity.field.batteryDischargePower",
  battery_power_inverted: "entity.field.batteryPowerInverted",
  site_current_signed: "entity.field.siteCurrentSigned",
  grid_power_inverted: "entity.field.gridPowerInverted",
  grid_power_source_power: "entity.field.gridPowerSource",
  grid_power_source_power_export: "entity.field.gridPowerSourceExport",
  max_age_s: "entity.field.maxAge",
};

export function fieldLabelKey(field: string): TranslationKey | null {
  return FIELD_LABELS[field] ?? null;
}

/** The sentence for each warning code a detected meter can carry; an unknown code says nothing. */
export const DETECT_WARNING_KEYS: Record<string, TranslationKey> = {
  own_load_balancing: "entity.detect.warning.own_load_balancing",
  sign_unverified: "entity.detect.warning.sign_unverified",
  voltage_from_other_device: "entity.detect.warning.voltage_from_other_device",
  may_measure_subcircuit: "entity.detect.warning.may_measure_subcircuit",
  reports_on_change_only: "entity.detect.warning.reports_on_change_only",
};

/** Detection notes that only inform; every other code asks the person to check something. */
export const INFORMATIONAL_DETECT_WARNINGS: ReadonlySet<string> = new Set([
  "voltage_from_other_device",
  "reports_on_change_only",
]);

/** Whether the site already uses this detected battery exactly as it would be applied. */
export function batteryApplied(config: EntityConfig, battery: DetectedBattery): boolean {
  const entity = (name: string): string => {
    const field = config.fields.find((entry) => entry.field === name);
    return field !== undefined && field.kind === "entity" && field.current !== null ? field.current.entityId : "";
  };
  const flagField = config.fields.find((entry) => entry.field === "battery_power_inverted");
  const inverted = flagField !== undefined && flagField.kind === "flag" ? flagField.value : false;
  return (
    entity("battery_aggregate_power_entity") === battery.entityId &&
    entity("battery_discharge_power_entity") === (battery.dischargeEntityId ?? "") &&
    inverted === battery.inverted
  );
}

export const DERIVED_KIND_KEYS: Record<string, TranslationKey> = {
  power: "entity.derived.power",
  power_export: "entity.derived.powerExport",
  reactive_power: "entity.derived.reactivePower",
  apparent_power: "entity.derived.apparentPower",
  current: "entity.derived.current",
  voltage: "entity.derived.voltage",
};

const FIELD_ERROR_KEYS: Record<string, TranslationKey> = {
  required: "entity.error.field.required",
  entity_not_found: "entity.error.field.notFound",
  wrong_domain: "entity.error.field.wrongDomain",
  invalid_value: "entity.error.field.invalid",
  not_writable: "entity.error.field.notWritable",
  control_path_unknown: "entity.error.field.controlPathUnknown",
  unknown_field: "entity.error.field.unknown",
  charge_control_in_use: "entity.error.field.chargeControlInUse",
  current_limit_in_use: "entity.error.field.currentLimitInUse",
  duplicate_charger: "entity.error.field.duplicateCharger",
  unknown_vehicle: "entity.error.field.unknownVehicle",
  invalid_capacity: "settings.vehicle.error.capacity",
  invalid_consumption: "settings.vehicle.error.consumption",
  invalid_onboard_phases: "settings.vehicle.error.onboardPhases",
};

export function fieldErrorKey(code: string): TranslationKey {
  return FIELD_ERROR_KEYS[code] ?? "entity.error.field.unknown";
}

/**
 * The sentence for an envelope code. `conflict` has its own wording because the caller adopts the
 * returned config; every other code leaves what was read as it was.
 */
export function entityErrorKey(code: string | null): TranslationKey {
  if (code === ENTITY_NOT_ADMIN) {
    return "entity.error.notAdmin";
  }
  if (code === ENTITY_CONFLICT) {
    return "entity.error.conflict";
  }
  if (code === ENTITY_INVALID_VALUE) {
    return "entity.error.invalid";
  }
  if (code === ENTITY_NO_SITE) {
    return "site.none";
  }
  return "entity.error.generic";
}

const FIELD_HELP: Record<string, TranslationKey> = {
  charge_control: "entity.help.chargeControl",
  current_limit: "entity.help.currentLimit",
  energy_register_entity: "entity.help.energyRegister",
  power_entity: "entity.help.powerEntity",
  vehicle_soc: "entity.help.vehicleSoc",
  main_fuse_a: "entity.help.mainFuse",
  safety_margin_a: "entity.help.safetyMargin",
  measurement_mode: "entity.help.measurementMode",
  voltage_between_phases_v: "entity.help.voltageBetweenPhases",
  charger_phases: "entity.help.chargerPhases",
  charger_priority: "entity.help.chargerPriority",
  battery_aggregate_power_entity: "entity.help.batteryPower",
  battery_discharge_power_entity: "entity.help.batteryDischargePower",
  battery_power_inverted: "entity.help.batteryPowerInverted",
  site_current_signed: "entity.help.siteCurrentSigned",
  grid_power_inverted: "entity.help.gridPowerInverted",
  grid_power_source_power: "entity.help.gridPowerSource",
  grid_power_source_power_export: "entity.help.gridPowerSourceExport",
  max_age_s: "entity.help.maxAge",
};

const DERIVED_HELP: Record<string, TranslationKey> = {
  power: "entity.help.derivedPower",
  power_export: "entity.help.derivedPowerExport",
  reactive_power: "entity.help.derivedReactivePower",
  apparent_power: "entity.help.derivedApparentPower",
  current: "entity.help.derivedCurrent",
  voltage: "entity.help.derivedVoltage",
};

export function fieldHelpKey(field: string): TranslationKey | null {
  const fixed = FIELD_HELP[field];
  if (fixed !== undefined) {
    return fixed;
  }
  if (/^direct_L[123]$/.test(field)) {
    return "entity.help.phaseDirect";
  }
  const derived = /^derived_L[123]_(.+)$/.exec(field);
  return derived === null ? null : (DERIVED_HELP[derived[1] ?? ""] ?? null);
}

/**
 * A configured entity Home Assistant no longer knows: the backend reports the stored id in place
 * of a friendly name when no state exists.
 */
export function isMissingEntity(field: EntityFieldEntity): boolean {
  return field.current !== null && !field.current.exists;
}

export function automaticEntity(field: EntityFieldEntity): EntityRef | null {
  const effective = field.effective;
  if (effective !== null && effective.source === "automatic") {
    return effective;
  }
  // The current limit also says what Automatic would find while None is chosen.
  return field.none === null ? null : field.none.automatic;
}
